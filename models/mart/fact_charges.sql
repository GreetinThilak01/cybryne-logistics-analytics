/*
    fact_charges — mart layer (§3.3.2)
    Clean projection of int_charges_classified. Grain: one row per cost or
    surcharge line per job. Denormalised customer/carrier/branch/mode keys so
    the table stars independently in Power BI.
*/

{{ config(materialized='table', schema='mart') }}

select
    charge_key,
    shipment_key,
    job_number,
    charge_type_key,
    customer_key,
    carrier_key,
    branch_key,
    mode_key,
    charge_date,
    vendor_name,
    amount_inr,
    is_post_closure

from {{ ref('int_charges_classified') }}
