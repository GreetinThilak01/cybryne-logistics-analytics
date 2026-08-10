/*
    fact_payments — mart layer (§3.3.4)
    Clean projection of stg_tally__payments (payments need no intermediate
    business logic; settlement math lives in int_invoice_settlement). Grain:
    one row per payment allocation to an invoice.
*/

{{ config(materialized='table', schema='mart') }}

select
    payment_key,
    receipt_number,
    invoice_number,
    customer_key,
    payment_date,
    amount_inr,
    payment_mode

from {{ ref('stg_tally__payments') }}
