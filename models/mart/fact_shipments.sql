/*
    fact_shipments — mart layer (§3.3.1)
    Clean projection of int_shipments_conformed; no new logic. Grain: one row
    per shipment (Logi-Sys job). Stable integer keys survive from the raw
    layer, so dbt_utils.generate_surrogate_key is deliberately not applied
    (see README — key strategy).
*/

{{ config(materialized='table', schema='mart') }}

select
    shipment_key,
    job_number,
    customer_key,
    carrier_key,
    branch_key,
    mode_key,
    origin_port_key,
    destination_port_key,
    trade_lane,
    destination_region,
    booking_date,
    committed_delivery_date,
    planned_etd,
    actual_departure_date,
    carrier_eta,
    actual_delivery_date,
    job_completion_date,
    shipment_status,
    commodity,
    incoterm,
    container_count,
    teu,
    chargeable_weight_kg,
    committed_transit_days,
    actual_transit_days,
    transit_variance_days,
    is_on_time,
    exception_count,
    has_exception

from {{ ref('int_shipments_conformed') }}
