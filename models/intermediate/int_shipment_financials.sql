/*
    int_shipment_financials
    -----------------------
    The join that changes the room (§6.2, §6.3, §6.12). Joins Logi-Sys costs
    to Tally revenue at job level. This calculation is only possible when
    both source systems are joined — neither system alone reveals the margin
    problem. Revenue from Tally invoice lines. Costs from Logi-Sys charge
    lines including post-closure surcharges. Gross margin = revenue_billed -
    total_cost.

    Grain: one row per completed job (jobs still open have no meaningful
    margin yet). Revenue is pre-tax and net of credit notes; only matched
    invoice lines count (unmatched lines are a visible data-quality issue,
    not silent revenue). surcharge_billed_inr vs surcharge_cost_inr are the
    two sides of §6.12 Surcharge Capture Rate.
*/

with shipments as (
    select
        shipment_key, job_number, customer_key, carrier_key, branch_key,
        mode_key, job_completion_date
    from {{ ref('int_shipments_conformed') }}
    where job_completion_date is not null
),

costs as (
    select
        shipment_key,
        sum(case when charge_category = 'Base Cost' then amount_inr else 0 end) as base_cost_inr,
        sum(case when charge_category = 'Surcharge' then amount_inr else 0 end) as surcharge_cost_inr
    from {{ ref('int_charges_classified') }}
    group by 1
),

charge_types as (
    select charge_type_key, charge_category
    from {{ ref('dim_charge_type') }}
),

revenue as (
    select
        l.shipment_key,
        sum(l.line_amount_inr) as revenue_billed_inr,
        sum(case when ct.charge_category = 'Surcharge'
                 then l.line_amount_inr else 0 end) as surcharge_billed_inr,
        min(case when l.invoice_type = 'Original'
                 then l.invoice_date end) as first_invoice_date,
        max(case when l.is_first_original_invoice
                 then l.billing_lag_days end) as billing_lag_days
    from {{ ref('int_invoices_matched') }} l
    left join charge_types ct on l.charge_type_key = ct.charge_type_key
    where l.is_matched
    group by 1
)

select
    s.shipment_key,
    s.job_number,
    s.customer_key,
    s.carrier_key,
    s.branch_key,
    s.mode_key,
    s.job_completion_date,
    r.first_invoice_date,
    r.billing_lag_days,
    round(coalesce(r.revenue_billed_inr, 0), 2)   as revenue_billed_inr,
    round(coalesce(c.base_cost_inr, 0), 2)        as base_cost_inr,
    round(coalesce(c.surcharge_cost_inr, 0), 2)   as surcharge_cost_inr,
    round(coalesce(r.surcharge_billed_inr, 0), 2) as surcharge_billed_inr,
    round(coalesce(c.base_cost_inr, 0) + coalesce(c.surcharge_cost_inr, 0), 2) as total_cost_inr,
    round(coalesce(r.revenue_billed_inr, 0)
          - coalesce(c.base_cost_inr, 0)
          - coalesce(c.surcharge_cost_inr, 0), 2) as gross_margin_inr,
    r.first_invoice_date is not null              as is_fully_billed

from shipments s
left join costs c on s.shipment_key = c.shipment_key
left join revenue r on s.shipment_key = r.shipment_key
