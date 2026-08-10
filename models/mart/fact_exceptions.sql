/*
    fact_exceptions — mart layer (§3.3.7)
    Selected directly from the raw source with type casting only: exception
    events carry no business-rule derivation (the blueprint's directory
    structure defines no staging model for them), so a pass-through with
    casts is the whole transformation. Grain: one row per exception event.
*/

{{ config(materialized='table', schema='mart') }}

select
    cast(exception_key as bigint)       as exception_key,
    cast(shipment_key as bigint)        as shipment_key,
    upper(trim(job_number))             as job_number,
    cast(exception_type_key as bigint)  as exception_type_key,
    cast(carrier_key as bigint)         as carrier_key,
    cast(customer_key as bigint)        as customer_key,
    cast(branch_key as bigint)          as branch_key,
    cast(raised_date as date)           as raised_date,
    cast(resolved_date as date)         as resolved_date,
    cast(delay_days_attributed as integer) as delay_days_attributed,
    trim(exception_status)              as exception_status

from {{ source('logisys_raw', 'exceptions') }}
