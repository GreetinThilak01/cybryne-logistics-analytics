/*
    stg_logisys__shipments
    ----------------------
    Source system   : Logi-Sys (Softlink Global)
    Extraction      : MIS report scheduler -> hourly CSV email export -> Cloud
                      Function -> raw layer (append-only)
    Transformation  : type casting, snake_case renaming, trim/upper on natural
                      key codes, null handling. NO business logic — is_on_time,
                      transit_variance_days and has_exception are deliberately
                      excluded here and re-derived in int_shipments_conformed,
                      so the agreed definitions live in exactly one place.
    Columns renamed : none differ in the synthetic raw layer; the rename block
                      below is where production Logi-Sys headers (e.g. "JobNo",
                      "ETD Date") map onto the blueprint standard.
    Scaffold note   : the synthetic raw layer carries surrogate keys; production
                      exports carry natural codes. The reference-seed joins
                      below map keys back to their source codes (customer_code,
                      carrier_code, port codes, mode) so downstream models and
                      tests read exactly as they would in production.
*/

with source as (
    select * from {{ source('logisys_raw', 'shipments') }}
),

customers as (select customer_key, customer_code from {{ ref('dim_customer') }}),
carriers  as (select carrier_key, carrier_code from {{ ref('dim_carrier') }}),
modes     as (select mode_key, mode, service_level from {{ ref('dim_mode') }}),
origins   as (select port_key, port_code from {{ ref('dim_origin_port') }}),
dests     as (select port_key, port_code from {{ ref('dim_destination_port') }})

select
    cast(s.shipment_key as bigint)              as shipment_key,
    upper(trim(s.job_number))                   as job_number,
    cast(s.customer_key as bigint)              as customer_key,
    upper(trim(c.customer_code))                as customer_code,
    cast(s.carrier_key as bigint)               as carrier_key,
    upper(trim(cr.carrier_code))                as carrier_code,
    cast(s.branch_key as bigint)                as branch_key,
    cast(s.mode_key as bigint)                  as mode_key,
    m.mode                                      as mode,
    m.service_level                             as service_level,
    cast(s.origin_port_key as bigint)           as origin_port_key,
    upper(trim(op.port_code))                   as origin_port_code,
    cast(s.destination_port_key as bigint)      as destination_port_key,
    upper(trim(dp.port_code))                   as destination_port_code,
    s.trade_lane,
    s.destination_region,
    cast(s.booking_date as date)                as booking_date,
    cast(s.committed_delivery_date as date)     as committed_delivery_date,
    cast(s.planned_etd as date)                 as planned_etd,
    cast(s.actual_departure_date as date)       as actual_departure_date,
    cast(s.carrier_eta as date)                 as carrier_eta,
    cast(s.actual_delivery_date as date)        as actual_delivery_date,
    cast(s.job_completion_date as date)         as job_completion_date,
    trim(s.shipment_status)                     as shipment_status,
    s.commodity,
    upper(trim(s.incoterm))                     as incoterm,
    cast(s.container_count as integer)          as container_count,
    cast(s.teu as decimal(10, 2))               as teu,
    cast(s.chargeable_weight_kg as decimal(12, 2)) as chargeable_weight_kg,
    cast(s.committed_transit_days as integer)   as committed_transit_days,
    cast(s.actual_transit_days as integer)      as actual_transit_days,
    cast(s.exception_count as integer)          as exception_count,
    current_timestamp                           as _loaded_at

from source s
left join customers c  on s.customer_key = c.customer_key
left join carriers cr  on s.carrier_key = cr.carrier_key
left join modes m      on s.mode_key = m.mode_key
left join origins op   on s.origin_port_key = op.port_key
left join dests dp     on s.destination_port_key = dp.port_key
