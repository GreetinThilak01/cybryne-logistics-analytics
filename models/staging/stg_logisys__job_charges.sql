/*
    stg_logisys__job_charges
    ------------------------
    Source system   : Logi-Sys (Softlink Global), costing module
    Extraction      : same hourly MIS CSV export -> Cloud Function -> raw layer
    Transformation  : type casting, code trim/upper, amount cast to NUMERIC.
                      charge_code and charge_category are carried as source
                      attributes; the full charge-type enrichment join
                      (is_passthrough_expected, charge_name) happens in
                      int_charges_classified. is_post_closure is deliberately
                      excluded — it is a business rule (§6.2) and is derived
                      in intermediate.
    Columns renamed : none in the synthetic raw layer (production: "ChargeHead"
                      -> charge_code, "Amt" -> amount_inr).
*/

with source as (
    select * from {{ source('logisys_raw', 'job_charges') }}
),

charge_types as (
    select charge_type_key, charge_code, charge_category
    from {{ ref('dim_charge_type') }}
)

select
    cast(s.charge_key as bigint)        as charge_key,
    cast(s.shipment_key as bigint)      as shipment_key,
    upper(trim(s.job_number))           as job_number,
    cast(s.charge_type_key as bigint)   as charge_type_key,
    upper(trim(ct.charge_code))         as charge_code,
    ct.charge_category                  as charge_category,
    cast(s.customer_key as bigint)      as customer_key,
    cast(s.carrier_key as bigint)       as carrier_key,
    cast(s.branch_key as bigint)        as branch_key,
    cast(s.mode_key as bigint)          as mode_key,
    cast(s.charge_date as date)         as charge_date,
    trim(s.vendor_name)                 as vendor_name,
    cast(s.amount_inr as decimal(14, 2)) as amount_inr,
    current_timestamp                   as _loaded_at

from source s
left join charge_types ct on s.charge_type_key = ct.charge_type_key
