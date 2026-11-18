# Worldwide higher-education institution catalogue

Derived from Research Organization Registry (ROR) Data, release v2.15, 20 October 2026.
Source: https://zenodo.org/records/23500708
Record data: https://ror.org/
Licence: Creative Commons CC0 1.0 Universal public domain dedication.
https://creativecommons.org/publicdomain/zero/1.0/

This derivative includes active ROR organizations with the `education` type and a complete country, subdivision and locality in their ROR location metadata. It contains 30,256 institution/location entries across 213 countries/territories. A multi-location organization can appear more than once. It is not an exhaustive national register and does not include every college, campus or vocational provider.

Full display names, supplied acronyms and alternate names are retained. Acronyms are never generated from initials. The `source_url` links to the original ROR record, and `source_id` combines the ROR identifier with the GeoNames locality identifier.

Country and subdivision labels are normalized to the bundled worldwide geography catalogue where ISO subdivision codes or unambiguous names match. FUNAAB is sourced from https://ror.org/050s1zm26 and maps to Nigeria, Ogun, Abeokuta.

Import using `python manage.py import_institutions`. Re-running refreshes sourced rows without duplicating source IDs or reactivating disabled institutions. Admin-only records remain supported. These are registered localities, not campus-gate coordinates or verified geographic-nearest calculations.
