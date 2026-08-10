# cybryne_logistics — dbt project

The transformation layer of the Cybryne freight-forwarder analytics platform
(see `../cybryne-logistics-bi-architecture-blueprint.md` for the full
architecture). It takes the raw feeds from the client's two source systems,
encodes the business definitions agreed with leadership as version-controlled
SQL, and builds the star-schema mart that Power BI imports hourly.

This scaffold runs the **entire pipeline locally on DuckDB** against the
synthetic dataset in `../output/` — a real DAG, real tests, real data. In
production the identical models run on **Google BigQuery**, triggered hourly
by Cloud Scheduler (`dbt source freshness && dbt run && dbt test`); only the
profile changes.

## Source systems

| Source | Access path | Entities |
|---|---|---|
| **Logi-Sys** (Softlink Global) | No public API. The MIS report scheduler emails hourly CSV exports to a dedicated mailbox; a Cloud Function validates, parses and appends them to the raw layer. | Shipments/jobs, job charges & surcharges, exceptions, plus the pipeline's own run log |
| **Tally Prime** | No REST API. Tally's native XML interface listens on localhost only, so a Python agent on the same machine queries it hourly and pushes vouchers to the raw layer over HTTPS. No firewall changes, no exposed ports. | Sales invoice lines, receipt allocations |

In this scaffold the raw layer is the set of CSVs in `../output/`, read in
place via DuckDB `external_location` sources. The eight dimension CSVs load
as **dbt seeds** — static reference data that comes from Logi-Sys
configuration exports in production.

## Four-layer architecture

| Layer | Why it exists |
|---|---|
| **Raw** (sources) | Immutable, append-only landing zone — exactly what the source sent, when it sent it, so history can be replayed and the committed delivery date can be first-seen-captured. |
| **Staging** (`stg_*`) | One typed, renamed, code-standardised model per source entity — two systems that named nothing the same way get a single naming standard, with zero business logic. |
| **Intermediate** (`int_*`) | The business-rule layer: every agreed definition lives here once, in tested SQL, including the first-ever join of operational and financial data. |
| **Mart** (`fact_*` + seed dims) | The star schema, shaped exclusively for Power BI — clean projections only, no logic that isn't already proven upstream. |

## Running locally

```bash
cd cybryne_logistics
python3 -m pip install dbt-duckdb

dbt deps                            # install dbt_utils
dbt seed  --profiles-dir .          # load the 8 dimension seeds
dbt run   --profiles-dir .          # build staging -> intermediate -> mart
dbt test  --profiles-dir .          # schema tests + the 13 case-study assertions
dbt docs generate --profiles-dir .  # lineage docs / DAG
dbt docs serve    --profiles-dir .  # browse the DAG
```

## Key business rules encoded in intermediate models

| Model | Rule (blueprint reference) |
|---|---|
| `int_shipments_conformed` | **§6.1 On-Time Delivery** — actual delivery vs the committed date from the *original booking confirmation*, first-seen and immutable; carrier ETA retained but never used. **§6.5 Transit Variance.** |
| `int_charges_classified` | **§6.2** — surcharges recorded after job closure still belong to the job and count against its margin (`is_post_closure`). |
| `int_invoices_matched` | Invoice→job matching via the voucher's job reference; unmatched lines (~1.5% by design) are flagged, never dropped. **§6.4 Billing Lag** from the first Original invoice. |
| `int_invoice_settlement` | **§6.9 Outstanding AR** — one authoritative row per invoice with pre-computed outstanding and ageing, so Power BI never nets payments in DAX. |
| `int_shipment_financials` | **§6.2/§6.3 Gross Margin** and **§6.12 Surcharge Capture** — the ops↔finance join that neither source system can produce alone. |

## Notes and deliberate choices

- **`tests/assert_case_study_outcomes.sql`** re-verifies all 13 case-study
  outcomes (§7.8) against the dbt-built mart on every `dbt test` — if a model
  change breaks the story the dashboards tell, the build fails.
- **Key strategy:** `dbt_utils.generate_surrogate_key` is not applied in the
  marts because stable integer keys survive from the raw layer and integer
  keys compress and join better in Power BI import mode. In the BigQuery
  build, dimension surrogate keys are minted in dbt the same way.
- **AR "as of" date** is pinned to `var: as_of_date` (2025-01-15, the
  synthetic snapshot date) so the build is deterministic; production swaps in
  `CURRENT_DATE`.
- **Source freshness** (`warn 2h / error 4h`) is defined per the blueprint but
  only meaningful against live hourly feeds — running it against static CSVs
  will report stale by design.
