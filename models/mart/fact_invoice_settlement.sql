/*
    fact_invoice_settlement — mart layer (§3.3.5)
    Clean projection of int_invoice_settlement. Grain: one row per receivable
    invoice (accumulating snapshot, rebuilt each run). Ageing is as of
    var('as_of_date') — CURRENT_DATE in production.
*/

{{ config(materialized='table', schema='mart') }}

select
    invoice_number,
    customer_key,
    branch_key,
    invoice_date,
    due_date,
    invoice_total_inr,
    amount_received_inr,
    outstanding_inr,
    last_payment_date,
    days_overdue,
    aging_bucket,
    is_settled

from {{ ref('int_invoice_settlement') }}
