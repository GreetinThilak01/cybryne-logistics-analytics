/*
    fact_shipment_financials — mart layer (§3.3.6)
    Clean projection of int_shipment_financials — the pre-joined ops↔finance
    table that powers the Customer Profitability Report. Grain: one row per
    completed job. Power BI never joins two fact tables at query time because
    this table already did it in version-controlled SQL.
*/

{{ config(materialized='table', schema='mart') }}

select
    shipment_key,
    job_number,
    customer_key,
    carrier_key,
    branch_key,
    mode_key,
    job_completion_date,
    first_invoice_date,
    billing_lag_days,
    revenue_billed_inr,
    base_cost_inr,
    surcharge_cost_inr,
    surcharge_billed_inr,
    total_cost_inr,
    gross_margin_inr,
    is_fully_billed

from {{ ref('int_shipment_financials') }}
