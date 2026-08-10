/*
    stg_tally__invoice_lines
    ------------------------
    Source system   : Tally Prime (sales vouchers)
    Extraction      : Python agent on the Tally host queries the native XML
                      interface on localhost hourly and pushes to the raw layer
                      over HTTPS (Tally refuses external connections by design)
    Transformation  : type casting, voucher_type standardised to the accepted
                      values (Original / Supplementary / Credit Note), code
                      trim. The job_number reference field is kept EXACTLY as
                      typed on the voucher — including the ~1.5% of narration
                      typos — because invoice->job matching is a business
                      process (§ int_invoices_matched), not a cleansing step.
                      shipment_key is deliberately NOT read from raw: matching
                      happens downstream via job_number, as it would against a
                      real Tally feed.
    Columns renamed : voucher_type -> invoice_type standardisation (production:
                      "VchType" -> invoice_type, "PartyLedger" -> ledger name).
*/

with source as (
    select * from {{ source('tally_raw', 'invoice_lines') }}
)

select
    cast(invoice_line_key as bigint)        as invoice_line_key,
    trim(invoice_number)                    as invoice_number,
    case
        when lower(trim(invoice_type)) = 'original'      then 'Original'
        when lower(trim(invoice_type)) = 'supplementary' then 'Supplementary'
        when lower(trim(invoice_type)) in ('credit note', 'creditnote', 'cn')
                                                         then 'Credit Note'
        else trim(invoice_type)
    end                                     as invoice_type,
    cast(invoice_date as date)              as invoice_date,
    cast(due_date as date)                  as due_date,
    cast(customer_key as bigint)            as customer_key,
    upper(trim(job_number))                 as job_number,
    cast(branch_key as bigint)              as branch_key,
    cast(charge_type_key as bigint)         as charge_type_key,
    line_description,
    cast(line_amount_inr as decimal(14, 2)) as line_amount_inr,
    cast(tax_amount_inr as decimal(14, 2))  as tax_amount_inr,
    cast(total_amount_inr as decimal(14, 2)) as total_amount_inr,
    current_timestamp                       as _loaded_at

from source
