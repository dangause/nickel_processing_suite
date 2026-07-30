#!/usr/bin/env python
"""
template_metadata.py - Track and query template date ranges

This module helps manage template metadata, particularly date ranges used
in template construction. Critical for avoiding contamination from transients.

Usage:
    # Record template metadata when building
    stips-archive-template-meta record \
        --repo $REPO \
        --collection templates/deep/r \
        --start 20210101 \
        --end 20210131

    # Query templates excluding certain date ranges
    stips-archive-template-meta query \
        --repo $REPO \
        --exclude-start 20210219 \
        --exclude-end 20210228

    # List all template metadata
    stips-archive-template-meta list --repo $REPO
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

#: Sentinel meaning "this template is an external survey product, not a date
#: range of our own observations". Historically the literal "PS1" (still
#: emitted for that source, so existing recorded metadata stays consistent);
#: "EXTERNAL" is the source-agnostic form emitted for every other external
#: survey (e.g. SkyMapper).
EXTERNAL_DATE_SENTINELS = {"PS1", "EXTERNAL"}

#: Internal (non-adapter) source values, not registered in the external-survey
#: adapter registry: "nickel" (our own coadd templates) and "hybrid".
_INTERNAL_SOURCES = ("nickel", "hybrid")


def _source_choices() -> list[str]:
    """Valid ``--source`` values: internal sources plus every registered
    external-survey adapter (e.g. "ps1", "skymapper"), so a new adapter
    (Task 5+) is automatically accepted here without a framework edit.
    """
    from stips.pipeline_tools.external_template.sources import SOURCES

    return [*_INTERNAL_SOURCES, *sorted(SOURCES)]


class TemplateMetadata:
    """Manager for template metadata including date ranges."""

    def __init__(self, repo_path: str):
        self.repo_path = Path(repo_path)
        self.metadata_file = self.repo_path / "template_metadata.json"
        self.metadata = self._load_metadata()

    def _load_metadata(self) -> dict:
        """Load existing metadata or create new."""
        if self.metadata_file.exists():
            with open(self.metadata_file, "r") as f:
                return json.load(f)
        return {"templates": {}}

    def _save_metadata(self):
        """Save metadata to file."""
        self.metadata_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.metadata_file, "w") as f:
            json.dump(self.metadata, f, indent=2)

    def record_template(
        self,
        collection: str,
        start_date: str,
        end_date: str,
        tract: Optional[str] = None,
        band: Optional[str] = None,
        description: Optional[str] = None,
        source: str = "nickel",
        ps1_filter: Optional[str] = None,
        ps1_ra: Optional[float] = None,
        ps1_dec: Optional[float] = None,
        ps1_cutout_size: Optional[float] = None,
    ):
        """Record metadata for a template collection.

        Parameters
        ----------
        collection : str
            Template collection name (e.g., "templates/deep/r" or "templates/ps1/r")
        start_date : str
            Earliest observation date (YYYYMMDD) or "PS1" for PS1 stacks
        end_date : str
            Latest observation date (YYYYMMDD) or "PS1" for PS1 stacks
        tract : str, optional
            Sky tract ID
        band : str, optional
            Filter band (Nickel: b, v, r, i)
        description : str, optional
            Human-readable description
        source : str
            Template source: "nickel" (internal), "ps1" (PanSTARRS), or "hybrid"
        ps1_filter : str, optional
            Original PS1 filter (g, r, i, z, y) for PS1 templates
        ps1_ra : float, optional
            PS1 cutout center RA (degrees)
        ps1_dec : float, optional
            PS1 cutout center Dec (degrees)
        ps1_cutout_size : float, optional
            PS1 cutout size (degrees)
        """
        # Validate dates (skip validation for external-survey templates, which
        # carry a sentinel instead of a real observation date range).
        if (
            start_date not in EXTERNAL_DATE_SENTINELS
            and end_date not in EXTERNAL_DATE_SENTINELS
        ):
            try:
                start = datetime.strptime(start_date, "%Y%m%d")
                end = datetime.strptime(end_date, "%Y%m%d")
                if start > end:
                    raise ValueError("start_date must be <= end_date")
            except ValueError as e:
                raise ValueError(f"Invalid date format or range: {e}")

        # Store metadata
        template_meta = {
            "start_date": start_date,
            "end_date": end_date,
            "tract": tract,
            "band": band,
            "description": description or f"Template from {start_date} to {end_date}",
            "created": datetime.now().isoformat(),
            "source": source,
        }

        # Add external-survey geometry metadata if applicable, filed under the
        # ACTUAL source name (not a hardcoded "ps1") so a SkyMapper template
        # isn't recorded as if it were PS1. "ps1" and "hybrid" reproduce the
        # historical decision boundary (source == "ps1", or any source when
        # ps1_filter is given); every other external source (e.g. skymapper)
        # gets its own geometry key even without an explicit ps1_filter.
        if source not in ("nickel", "hybrid") or ps1_filter:
            template_meta[source] = {
                "filter": ps1_filter,
                "ra": ps1_ra,
                "dec": ps1_dec,
                "cutout_size_deg": ps1_cutout_size,
            }

        self.metadata["templates"][collection] = template_meta

        self._save_metadata()
        print(f"Recorded metadata for {collection}")
        print(f"  Source: {source}")
        print(f"  Date range: {start_date} - {end_date}")
        if tract:
            print(f"  Tract: {tract}")
        if band:
            print(f"  Band: {band}")
        if ps1_filter:
            print(f"  PS1 filter: {ps1_filter}")

    def query_templates(
        self,
        exclude_start: Optional[str] = None,
        exclude_end: Optional[str] = None,
        required_band: Optional[str] = None,
        required_tract: Optional[str] = None,
    ) -> list[str]:
        """Query templates that don't overlap with exclusion date range.

        Parameters
        ----------
        exclude_start : str, optional
            Start of date range to exclude (YYYYMMDD)
        exclude_end : str, optional
            End of date range to exclude (YYYYMMDD)
        required_band : str, optional
            Only return templates in this band
        required_tract : str, optional
            Only return templates for this tract

        Returns
        -------
        list[str]
            List of template collection names that match criteria
        """
        if exclude_start and exclude_end:
            try:
                excl_start = datetime.strptime(exclude_start, "%Y%m%d")
                excl_end = datetime.strptime(exclude_end, "%Y%m%d")
            except ValueError as e:
                raise ValueError(f"Invalid exclusion date format: {e}")
        else:
            excl_start = excl_end = None

        matching = []
        for collection, meta in self.metadata["templates"].items():
            # Check band filter
            if required_band and meta.get("band") != required_band:
                continue

            # Check tract filter
            if required_tract and meta.get("tract") != required_tract:
                continue

            # Check date overlap
            if excl_start and excl_end:
                try:
                    tmpl_start = datetime.strptime(meta["start_date"], "%Y%m%d")
                    tmpl_end = datetime.strptime(meta["end_date"], "%Y%m%d")
                except ValueError:
                    # Non-date entries (e.g., PS1) are treated as always-valid for exclusion logic
                    matching.append(collection)
                    continue

                # Check if template overlaps with exclusion range
                # No overlap if: template ends before exclusion starts OR template starts after exclusion ends
                no_overlap = tmpl_end < excl_start or tmpl_start > excl_end

                if not no_overlap:
                    continue  # Skip this template (it overlaps with exclusion)

            matching.append(collection)

        return matching

    def list_templates(
        self, verbose: bool = False, source_filter: Optional[str] = None
    ):
        """Print all template metadata.

        Parameters
        ----------
        verbose : bool
            Include all metadata fields
        source_filter : str, optional
            Filter by source: "nickel", "ps1", or "hybrid"
        """
        if not self.metadata["templates"]:
            print("No template metadata found.")
            print(f"Metadata file: {self.metadata_file}")
            return

        # Filter by source if requested
        templates = self.metadata["templates"]
        if source_filter:
            templates = {
                k: v
                for k, v in templates.items()
                if v.get("source", "nickel") == source_filter
            }

        if not templates:
            print(f"No templates found with source='{source_filter}'")
            return

        print(f"\nTemplate Metadata ({len(templates)} templates)")
        if source_filter:
            print(f"Filtered by source: {source_filter}")
        print(f"File: {self.metadata_file}")
        print("=" * 80)

        for collection, meta in sorted(templates.items()):
            source = meta.get("source", "nickel")
            print(f"\n{collection} [{source}]")
            print(f"  Date range: {meta['start_date']} - {meta['end_date']}")

            if meta.get("band"):
                print(f"  Band: {meta['band']}")
            if meta.get("tract"):
                print(f"  Tract: {meta['tract']}")

            # External-survey geometry info, filed under the source name
            # (e.g. "ps1", "skymapper") rather than a hardcoded "ps1" key.
            survey_info = meta.get(source)
            if survey_info:
                if survey_info.get("filter"):
                    print(f"  {source} filter: {survey_info['filter']}")
                if survey_info.get("ra") and survey_info.get("dec"):
                    print(
                        f"  {source} position: RA={survey_info['ra']:.4f}, "
                        f"Dec={survey_info['dec']:.4f}"
                    )
                if survey_info.get("cutout_size_deg"):
                    print(
                        f"  {source} cutout size: {survey_info['cutout_size_deg']:.3f} deg"
                    )

            if meta.get("description"):
                print(f"  Description: {meta['description']}")

            if verbose and meta.get("created"):
                print(f"  Created: {meta['created']}")

        print()


def main():
    parser = argparse.ArgumentParser(
        description="Manage template metadata for DIA processing",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # Record command
    record_parser = subparsers.add_parser(
        "record", help="Record metadata for a template collection"
    )
    record_parser.add_argument("--repo", required=True, help="Butler repository path")
    record_parser.add_argument(
        "--collection", required=True, help="Template collection name"
    )
    record_parser.add_argument(
        "--start", required=True, help="Start date (YYYYMMDD) or 'PS1'"
    )
    record_parser.add_argument(
        "--end", required=True, help="End date (YYYYMMDD) or 'PS1'"
    )
    record_parser.add_argument("--tract", help="Sky tract ID")
    record_parser.add_argument("--band", help="Filter band")
    record_parser.add_argument("--description", help="Description of template")
    record_parser.add_argument(
        "--source",
        default="nickel",
        choices=_source_choices(),
        help="Template source (default: nickel)",
    )
    record_parser.add_argument(
        "--ps1-filter", help="Original PS1 filter (g, r, i, z, y)"
    )
    record_parser.add_argument("--ps1-ra", type=float, help="PS1 cutout RA (degrees)")
    record_parser.add_argument("--ps1-dec", type=float, help="PS1 cutout Dec (degrees)")
    record_parser.add_argument(
        "--ps1-size", type=float, help="PS1 cutout size (degrees)"
    )

    # Query command
    query_parser = subparsers.add_parser(
        "query", help="Query templates with date filtering"
    )
    query_parser.add_argument("--repo", required=True, help="Butler repository path")
    query_parser.add_argument(
        "--exclude-start",
        help="Exclude templates overlapping this start date (YYYYMMDD)",
    )
    query_parser.add_argument(
        "--exclude-end", help="Exclude templates overlapping this end date (YYYYMMDD)"
    )
    query_parser.add_argument("--band", help="Filter to specific band")
    query_parser.add_argument("--tract", help="Filter to specific tract")

    # List command
    list_parser = subparsers.add_parser("list", help="List all template metadata")
    list_parser.add_argument("--repo", required=True, help="Butler repository path")
    list_parser.add_argument(
        "--verbose", "-v", action="store_true", help="Show all metadata"
    )
    list_parser.add_argument(
        "--source",
        choices=_source_choices(),
        help="Filter by template source",
    )

    args = parser.parse_args()

    # Initialize manager
    metadata_mgr = TemplateMetadata(args.repo)

    # Execute command
    if args.command == "record":
        metadata_mgr.record_template(
            collection=args.collection,
            start_date=args.start,
            end_date=args.end,
            tract=args.tract,
            band=args.band,
            description=args.description,
            source=args.source,
            ps1_filter=args.ps1_filter,
            ps1_ra=args.ps1_ra,
            ps1_dec=args.ps1_dec,
            ps1_cutout_size=args.ps1_size,
        )

    elif args.command == "query":
        if args.exclude_start and not args.exclude_end:
            print("ERROR: --exclude-start requires --exclude-end", file=sys.stderr)
            sys.exit(1)
        if args.exclude_end and not args.exclude_start:
            print("ERROR: --exclude-end requires --exclude-start", file=sys.stderr)
            sys.exit(1)

        matching = metadata_mgr.query_templates(
            exclude_start=args.exclude_start,
            exclude_end=args.exclude_end,
            required_band=args.band,
            required_tract=args.tract,
        )

        if not matching:
            print("No templates match the criteria")
            sys.exit(0)

        print(f"\nMatching templates ({len(matching)}):")
        for collection in matching:
            meta = metadata_mgr.metadata["templates"][collection]
            print(f"  {collection}")
            print(f"    Date range: {meta['start_date']} - {meta['end_date']}")

    elif args.command == "list":
        metadata_mgr.list_templates(verbose=args.verbose, source_filter=args.source)


if __name__ == "__main__":
    main()
