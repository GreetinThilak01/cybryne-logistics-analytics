/*
    int_invoices_matched
    --------------------
    Matches Tally invoice lines to Logi-Sys jobs via the job_number reference
    field on the voucher. Unmatched invoices (~1.5% by design) are flagged,
    not dropped — they feed the data quality card on the Finance Dashboard.
    Derives billing_lag_days per §6.4: first Original invoice date minus
    job_completion_date.

    Matching is a LEFT join on the voucher's job reference exactly as typed:
    a narration typo fails the join and surfaces as is_matched = false. That
    is the honest behaviour of the production pipeline — data-entry problems
    are made visible, not silently repaired.
*/

with invoice_lines as (
    select * from {{ ref('stg_tally__invoice_lines') }}
),

shipments as (
    select shipment_key, job_number, job_completion_date
    from {{ ref('int_shipments_conformed') }}
),

matched as (
    select
        l.*,
        s.shipment_key as matched_shipment_key,
        s.job_completion_date,
        s.shipment_key is not null as is_matched
    from invoice_lines l
    left join shipments s on l.job_number = s.job_number
),

-- §6.4: identify the FIRST Original invoice per job. dense_rank over invoice
-- date + number (not row_number over lines) so all lines of the first invoice
-- carry the flag, regardless of how many lines it has.
ranked as (
    select
        m.*,
        case
            when m.invoice_type = 'Original' and m.is_matched then
                dense_rank() over (
                    partition by m.matched_shipment_key,
                                 case when m.invoice_type = 'Original' then 1 else 0 end
                    order by m.invoice_date, m.invoice_number
                )
        end as original_invoice_rank
    from matched m
)

select
    invoice_line_key,
    invoice_number,
    invoice_type,
    invoice_date,
    due_date,
    customer_key,
    matched_shipment_key as shipment_key,
    job_number,
    branch_key,
    charge_type_key,
    line_description,
    line_amount_inr,
    tax_amount_inr,
    total_amount_inr,
    is_matched,
    job_completion_date,
    coalesce(original_invoice_rank = 1, false) as is_first_original_invoice,
    case
        when original_invoice_rank = 1
            then date_diff('day', job_completion_date, invoice_date)
    end as billing_lag_days

from ranked
