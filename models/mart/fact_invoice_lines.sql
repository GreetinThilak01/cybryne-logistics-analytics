/*
    fact_invoice_lines — mart layer (§3.3.3)
    Clean projection of int_invoices_matched. Grain: one row per invoice line.
    shipment_key is null on deliberately-unmatched lines (~1.5%) — these feed
    the data-quality card on the Finance Dashboard.
*/

{{ config(materialized='table', schema='mart') }}

select
    invoice_line_key,
    invoice_number,
    invoice_type,
    invoice_date,
    due_date,
    customer_key,
    shipment_key,
    job_number,
    branch_key,
    charge_type_key,
    line_description,
    line_amount_inr,
    tax_amount_inr,
    total_amount_inr

from {{ ref('int_invoices_matched') }}
