/*
    fact_pipeline_runs — mart layer (§3.3.8)
    Monitoring fact for the Technical Notes page. Selected directly from the
    pipeline's own run log with casts only; deliberately unrelated to the
    business star (only dim_date joins it in Power BI). Grain: one row per
    pipeline execution per source per hour.
*/

{{ config(materialized='table', schema='mart') }}

select
    cast(run_key as bigint)             as run_key,
    cast(run_timestamp as timestamp)    as run_timestamp,
    cast(run_date as date)              as run_date,
    trim(source_system)                 as source_system,
    trim(pipeline_stage)                as pipeline_stage,
    trim(batch_id)                      as batch_id,
    cast(records_processed as integer)  as records_processed,
    cast(records_rejected as integer)   as records_rejected,
    cast(execution_seconds as decimal(10, 1)) as execution_seconds,
    trim(run_status)                    as run_status,
    error_message

from {{ source('logisys_raw', 'pipeline_runs') }}
