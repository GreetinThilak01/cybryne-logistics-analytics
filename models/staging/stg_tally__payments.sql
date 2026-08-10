/*
    stg_tally__payments
    -------------------
    Source system   : Tally Prime (receipt vouchers)
    Extraction      : same on-prem Python agent, hourly HTTPS push
    Transformation  : type casting only. One row per payment allocation to an
                      invoice (a receipt split across invoices lands as
                      multiple allocations, as Tally records it).
    Columns renamed : none in the synthetic raw layer (production: "RcptNo" ->
                      receipt_number, "InstrumentType" -> payment_mode).
*/

with source as (
    select * from {{ source('tally_raw', 'payments') }}
)

select
    cast(payment_key as bigint)         as payment_key,
    trim(receipt_number)                as receipt_number,
    trim(invoice_number)                as invoice_number,
    cast(customer_key as bigint)        as customer_key,
    cast(payment_date as date)          as payment_date,
    cast(amount_inr as decimal(14, 2))  as amount_inr,
    trim(payment_mode)                  as payment_mode,
    current_timestamp                   as _loaded_at

from source
