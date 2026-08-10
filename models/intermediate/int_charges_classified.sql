/*
    int_charges_classified
    ----------------------
    Encodes §6.2 Gross Margin rule: surcharges incurred after job closure
    still belong to the job and count against margin. is_post_closure flags
    charges where charge_date > job_completion_date.

    Joins the charge-type dimension on charge_code (the natural key the
    Logi-Sys export carries) to classify every line as Base Cost vs Surcharge
    and to carry is_passthrough_expected for the §6.12 capture-rate logic.
*/

with charges as (
    select * from {{ ref('stg_logisys__job_charges') }}
),

charge_types as (
    select charge_code, charge_name, charge_category, is_passthrough_expected
    from {{ ref('dim_charge_type') }}
),

shipments as (
    select job_number, job_completion_date
    from {{ ref('int_shipments_conformed') }}
)

select
    c.charge_key,
    c.shipment_key,
    c.job_number,
    c.charge_type_key,
    c.charge_code,
    ct.charge_name,
    ct.charge_category,
    ct.is_passthrough_expected,
    c.customer_key,
    c.carrier_key,
    c.branch_key,
    c.mode_key,
    c.charge_date,
    c.vendor_name,
    c.amount_inr,

    -- §6.2: a surcharge recorded after the job was operationally closed still
    -- belongs to that job. This is the mechanism that made the two problem
    -- customers invisible in either source system alone.
    coalesce(
        s.job_completion_date is not null and c.charge_date > s.job_completion_date,
        false
    ) as is_post_closure

from charges c
left join charge_types ct on c.charge_code = ct.charge_code
left join shipments s on c.job_number = s.job_number
