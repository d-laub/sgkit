import functools
import itertools
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Hashable, Iterator, MutableMapping, Optional, Sequence, Union

import dask
import fsspec
import numpy as np
import xarray as xr
from cyvcf2 import VCF, Variant

from sgkit.io.utils import zarrs_to_dataset
from sgkit.io.vcf import partition_into_regions
from sgkit.io.vcf.utils import build_url, chunks, temporary_directory, url_filename
from sgkit.model import DIM_VARIANT, create_genotype_call_dataset
from sgkit.typing import PathType

DEFAULT_MAX_ALT_ALLELES = (
    3  # equivalent to DEFAULT_ALT_NUMBER in vcf_read.py in scikit_allel
)


@contextmanager
def open_vcf(path: PathType) -> Iterator[VCF]:
    """A context manager for opening a VCF file."""
    vcf = VCF(path)
    try:
        yield vcf
    finally:
        vcf.close()


def region_filter(
    variants: Iterator[Variant], region: Optional[str] = None
) -> Iterator[Variant]:
    """Filter out variants that don't start in the given region."""
    if region is None:
        return variants
    else:
        start = get_region_start(region)
        return itertools.filterfalse(lambda v: v.POS < start, variants)


def get_region_start(region: str) -> int:
    """Return the start position of the region string."""
    if ":" not in region:
        return 1
    contig, start_end = region.split(":")
    start, end = start_end.split("-")
    return int(start)


def vcf_to_zarr_sequential(
    input: PathType,
    output: Union[PathType, MutableMapping[str, bytes]],
    region: Optional[str] = None,
    chunk_length: int = 10_000,
    chunk_width: int = 1_000,
    ploidy: int = 2,
    mixed_ploidy: bool = False,
    truncate_calls: bool = False,
    max_alt_alleles: int = DEFAULT_MAX_ALT_ALLELES,
) -> None:

    with open_vcf(input) as vcf:
        sample_id = np.array(vcf.samples, dtype=str)
        n_sample = len(sample_id)
        n_allele = max_alt_alleles + 1

        variant_contig_names = vcf.seqnames

        # Remember max lengths of variable-length strings
        max_variant_id_length = 0
        max_variant_allele_length = 0

        # Iterate through variants in batches of chunk_length

        if region is None:
            variants = vcf
        else:
            variants = vcf(region)

        variant_contig = np.empty(chunk_length, dtype="i1")
        variant_position = np.empty(chunk_length, dtype="i4")
        call_genotype = np.empty((chunk_length, n_sample, ploidy), dtype="i1")
        call_genotype_phased = np.empty((chunk_length, n_sample), dtype=bool)

        first_variants_chunk = True
        for variants_chunk in chunks(region_filter(variants, region), chunk_length):

            variant_ids = []
            variant_allele = []

            for i, variant in enumerate(variants_chunk):
                if variant.genotype is None:
                    raise ValueError("Genotype information missing from VCF.")
                variant_id = variant.ID if variant.ID is not None else "."
                variant_ids.append(variant_id)
                max_variant_id_length = max(max_variant_id_length, len(variant_id))
                try:
                    variant_contig[i] = variant_contig_names.index(variant.CHROM)
                except ValueError:
                    raise ValueError(
                        f"Contig '{variant.CHROM}' is not defined in the header."
                    )
                variant_position[i] = variant.POS

                alleles = [variant.REF] + variant.ALT
                if len(alleles) > n_allele:
                    alleles = alleles[:n_allele]
                elif len(alleles) < n_allele:
                    alleles = alleles + ([""] * (n_allele - len(alleles)))
                variant_allele.append(alleles)
                max_variant_allele_length = max(
                    max_variant_allele_length, max(len(x) for x in alleles)
                )

                fill = -2 if mixed_ploidy else -1
                gt = variant.genotype.array(fill=fill)
                gt_length = gt.shape[-1] - 1  # final element indicates phasing
                if (gt_length > ploidy) and not truncate_calls:
                    raise ValueError("Genotype call longer than ploidy.")
                n = min(call_genotype.shape[-1], gt_length)
                call_genotype[i, ..., 0:n] = gt[..., 0:n]
                call_genotype[i, ..., n:] = fill
                call_genotype_phased[i] = gt[..., -1]

            # Truncate np arrays (if last chunk is smaller than chunk_length)
            if i + 1 < chunk_length:
                variant_contig = variant_contig[: i + 1]
                variant_position = variant_position[: i + 1]
                call_genotype = call_genotype[: i + 1]
                call_genotype_phased = call_genotype_phased[: i + 1]

            variant_id = np.array(variant_ids, dtype="O")
            variant_id_mask = variant_id == "."
            variant_allele = np.array(variant_allele, dtype="O")

            ds: xr.Dataset = create_genotype_call_dataset(
                variant_contig_names=variant_contig_names,
                variant_contig=variant_contig,
                variant_position=variant_position,
                variant_allele=variant_allele,
                sample_id=sample_id,
                call_genotype=call_genotype,
                call_genotype_phased=call_genotype_phased,
                variant_id=variant_id,
                mixed_ploidy=mixed_ploidy,
            )
            ds["variant_id_mask"] = (
                [DIM_VARIANT],
                variant_id_mask,
            )
            ds.attrs["max_variant_id_length"] = max_variant_id_length
            ds.attrs["max_variant_allele_length"] = max_variant_allele_length

            if first_variants_chunk:
                # Enforce uniform chunks in the variants dimension
                # Also chunk in the samples direction

                def get_chunk_size(dim: Hashable, size: int) -> int:
                    if dim == "variants":
                        return chunk_length
                    elif dim == "samples":
                        return chunk_width
                    else:
                        return size

                encoding = {}
                for var in ds.data_vars:
                    var_chunks = tuple(
                        get_chunk_size(dim, size)
                        for (dim, size) in zip(ds[var].dims, ds[var].shape)
                    )
                    encoding[var] = dict(chunks=var_chunks)

                ds.to_zarr(output, mode="w", encoding=encoding)
                first_variants_chunk = False
            else:
                # Append along the variants dimension
                ds.to_zarr(output, append_dim=DIM_VARIANT)


def vcf_to_zarr_parallel(
    input: Union[PathType, Sequence[PathType]],
    output: Union[PathType, MutableMapping[str, bytes]],
    regions: Union[None, Sequence[str], Sequence[Optional[Sequence[str]]]],
    chunk_length: int = 10_000,
    chunk_width: int = 1_000,
    temp_chunk_length: Optional[int] = None,
    tempdir: Optional[PathType] = None,
    tempdir_storage_options: Optional[Dict[str, str]] = None,
    dask_fuse_avg_width: int = 50,
    ploidy: int = 2,
    mixed_ploidy: bool = False,
    truncate_calls: bool = False,
    max_alt_alleles: int = DEFAULT_MAX_ALT_ALLELES,
) -> None:
    """Convert specified regions of one or more VCF files to zarr files, then concat, rechunk, write to zarr"""

    if temp_chunk_length is None:
        temp_chunk_length = chunk_length

    with temporary_directory(
        prefix="vcf_to_zarr_", dir=tempdir, storage_options=tempdir_storage_options
    ) as tmpdir:

        paths = vcf_to_zarrs(
            input,
            tmpdir,
            regions,
            temp_chunk_length,
            chunk_width,
            tempdir_storage_options,
            ploidy=ploidy,
            mixed_ploidy=mixed_ploidy,
            truncate_calls=truncate_calls,
            max_alt_alleles=max_alt_alleles,
        )

        ds = zarrs_to_dataset(paths, chunk_length, chunk_width, tempdir_storage_options)

        # Ensure Dask task graph is efficient, see https://github.com/dask/dask/issues/5105
        with dask.config.set({"optimization.fuse.ave-width": dask_fuse_avg_width}):
            ds.to_zarr(output, mode="w")


def vcf_to_zarrs(
    input: Union[PathType, Sequence[PathType]],
    output: PathType,
    regions: Union[None, Sequence[str], Sequence[Optional[Sequence[str]]]],
    chunk_length: int = 10_000,
    chunk_width: int = 1_000,
    output_storage_options: Optional[Dict[str, str]] = None,
    ploidy: int = 2,
    mixed_ploidy: bool = False,
    truncate_calls: bool = False,
    max_alt_alleles: int = DEFAULT_MAX_ALT_ALLELES,
) -> Sequence[str]:
    """Convert VCF files to multiple Zarr on-disk stores, one per region.

    Parameters
    ----------
    input
        A path (or paths) to the input BCF or VCF file (or files). VCF files should
        be compressed and have a ``.tbi`` or ``.csi`` index file. BCF files should
        have a ``.csi`` index file.
    output
        Path to directory containing the multiple Zarr output stores.
    regions
        Genomic region or regions to extract variants for. For multiple inputs, multiple
        input regions are specified as a sequence of values which may be None, or a
        sequence of region strings.
    chunk_length
        Length (number of variants) of chunks in which data are stored, by default 10,000.
    chunk_width
        Width (number of samples) to use when storing chunks in output, by default 1,000.
    output_storage_options
        Any additional parameters for the storage backend, for the output (see ``fsspec.open``).
    ploidy
        The (maximum) ploidy of genotypes in the VCF file.
    mixed_ploidy
        If True, genotype calls with fewer alleles than the specified ploidy will be padded
        with the non-allele sentinel value of -2. If false, calls with fewer alleles than
        the specified ploidy will be treated as incomplete and will be padded with the
        missing-allele sentinel value of -1.
    truncate_calls
        If True, genotype calls with more alleles than the specified (maximum) ploidy value
        will be truncated to size ploidy. If false, calls with more alleles than the
        specified ploidy will raise an exception.
    max_alt_alleles
        The (maximum) number of alternate alleles in the VCF file. Any records with more than
        this number of alternate alleles will have the extra alleles dropped.

    Returns
    -------
    A list of URLs to the Zarr outputs.
    """

    output_storage_options = output_storage_options or {}

    if isinstance(input, str) or isinstance(input, Path):
        # Single input
        inputs: Sequence[PathType] = [input]
        assert regions is not None  # this would just be sequential case
        input_regions: Sequence[Optional[Sequence[str]]] = [regions]  # type: ignore
    else:
        # Multiple inputs
        inputs = input
        if regions is None:
            input_regions = [None] * len(inputs)
        else:
            if len(regions) == 0 or isinstance(regions[0], str):
                raise ValueError(
                    f"For multiple inputs, multiple input regions must be a sequence of sequence of strings: {regions}"
                )
            input_regions = regions

    assert len(inputs) == len(input_regions)

    tasks = []
    parts = []
    for i, input in enumerate(inputs):
        filename = url_filename(str(input))
        input_region_list = input_regions[i]
        if input_region_list is None:
            # single partition case: make a list so the loop below works
            input_region_list = [None]  # type: ignore
        for r, region in enumerate(input_region_list):
            part_url = build_url(str(output), f"{filename}/part-{r}.zarr")
            output_part = fsspec.get_mapper(part_url, **output_storage_options)
            parts.append(part_url)
            task = dask.delayed(vcf_to_zarr_sequential)(
                input,
                output=output_part,
                region=region,
                chunk_length=chunk_length,
                chunk_width=chunk_width,
                ploidy=ploidy,
                mixed_ploidy=mixed_ploidy,
                truncate_calls=truncate_calls,
                max_alt_alleles=max_alt_alleles,
            )
            tasks.append(task)
    dask.compute(*tasks)
    return parts


def vcf_to_zarr(
    input: Union[PathType, Sequence[PathType]],
    output: Union[PathType, MutableMapping[str, bytes]],
    *,
    target_part_size: Union[None, int, str] = "auto",
    regions: Union[None, Sequence[str], Sequence[Optional[Sequence[str]]]] = None,
    chunk_length: int = 10_000,
    chunk_width: int = 1_000,
    temp_chunk_length: Optional[int] = None,
    tempdir: Optional[PathType] = None,
    tempdir_storage_options: Optional[Dict[str, str]] = None,
    ploidy: int = 2,
    mixed_ploidy: bool = False,
    truncate_calls: bool = False,
    max_alt_alleles: int = DEFAULT_MAX_ALT_ALLELES,
) -> None:
    """Convert VCF files to a single Zarr on-disk store.

    By default, the conversion is carried out in parallel, by writing the output for each
    part to a separate, intermediate Zarr store in ``tempdir``. Then, in a second step
    the intermediate outputs are concatenated and rechunked into the final output Zarr
    store in ``output``.

    Conversion is carried out sequentially if ``target_part_size`` is None, and ``regions``
    is None.

    For more control over these two steps, consider using :func:`vcf_to_zarrs` followed by
    :func:`zarrs_to_dataset`, then saving the dataset using Xarray's
    :meth:`xarray.Dataset.to_zarr` method.

    Parameters
    ----------
    input
        A path (or paths) to the input BCF or VCF file (or files). VCF files should
        be compressed and have a ``.tbi`` or ``.csi`` index file. BCF files should
        have a ``.csi`` index file.
    output
        Zarr store or path to directory in file system.
    target_part_size
        The desired size, in bytes, of each (compressed) part of the input to be
        processed in parallel. Defaults to ``"auto"``, which will pick a good size
        (currently 100MB). A value of None means that the input will be processed
        sequentially. The setting will be ignored if ``regions`` is also specified.
    regions
        Genomic region or regions to extract variants for. For multiple inputs, multiple
        input regions are specified as a sequence of values which may be None, or a
        sequence of region strings. Takes priority over ``target_part_size`` if both
        are not None.
    chunk_length
        Length (number of variants) of chunks in which data are stored, by default 10,000.
    chunk_width
        Width (number of samples) to use when storing chunks in output, by default 1,000.
    temp_chunk_length
        Length (number of variants) of chunks for temporary intermediate files. Set this
        to be smaller than ``chunk_length`` to avoid memory errors when loading files with
        very large numbers of samples. Must be evenly divisible into ``chunk_length``.
        Defaults to ``chunk_length`` if not set.
    tempdir
        Temporary directory where intermediate files are stored. The default None means
        use the system default temporary directory.
    tempdir_storage_options:
        Any additional parameters for the storage backend for tempdir (see ``fsspec.open``).
    ploidy
        The (maximum) ploidy of genotypes in the VCF file.
    mixed_ploidy
        If True, genotype calls with fewer alleles than the specified ploidy will be padded
        with the non-allele sentinel value of -2. If false, calls with fewer alleles than
        the specified ploidy will be treated as incomplete and will be padded with the
        missing-allele sentinel value of -1.
    truncate_calls
        If True, genotype calls with more alleles than the specified (maximum) ploidy value
        will be truncated to size ploidy. If false, calls with more alleles than the
        specified ploidy will raise an exception.
    max_alt_alleles
        The (maximum) number of alternate alleles in the VCF file. Any records with more than
        this number of alternate alleles will have the extra alleles dropped.
    """

    if temp_chunk_length is not None:
        if chunk_length % temp_chunk_length != 0:
            raise ValueError(
                f"Temporary chunk length in variant dimension ({temp_chunk_length}) "
                f"must evenly divide target chunk length {chunk_length}"
            )
    if regions is None and target_part_size is not None:
        if target_part_size == "auto":
            target_part_size = "100MB"
        if isinstance(input, str) or isinstance(input, Path):
            regions = partition_into_regions(input, target_part_size=target_part_size)
        else:
            # Multiple inputs
            inputs = input
            regions = [
                partition_into_regions(input, target_part_size=target_part_size)
                for input in inputs
            ]

    if (isinstance(input, str) or isinstance(input, Path)) and (
        regions is None or isinstance(regions, str)
    ):
        convert_func = vcf_to_zarr_sequential
    else:
        convert_func = functools.partial(
            vcf_to_zarr_parallel,
            temp_chunk_length=temp_chunk_length,
            tempdir=tempdir,
            tempdir_storage_options=tempdir_storage_options,
        )
    convert_func(
        input,  # type: ignore
        output,
        regions,  # type: ignore
        chunk_length=chunk_length,
        chunk_width=chunk_width,
        ploidy=ploidy,
        mixed_ploidy=mixed_ploidy,
        truncate_calls=truncate_calls,
        max_alt_alleles=max_alt_alleles,
    )


def count_variants(path: PathType, region: Optional[str] = None) -> int:
    """Count the number of variants in a VCF file."""
    with open_vcf(path) as vcf:
        if region is not None:
            vcf = vcf(region)
        return sum(1 for _ in region_filter(vcf, region))
