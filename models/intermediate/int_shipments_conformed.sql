/*
    int_shipments_conformed
    -----------------------
    Encodes §6.1 On-Time Delivery definition agreed in the leadership working
    session — actual_delivery_date vs committed_delivery_date from the
    original booking confirmation. Committed date is the first-seen value per
    job from the raw layer (immutable once set). Carrier ETA is retained as a
    separate column to demonstrate the definition divergence but never used
    in OTD calculations.

    Also encodes §6.5 Transit Variance (actual - committed transit days).

    First-seen note: the synthetic raw layer carries one snapshot per job, so
    the dedup is a no-op here; against live hourly exports this model takes
    min_by(committed_delivery_date, _loaded_at) per job_number so later
    booking amendments can never restate the promise (blueprint §10.2).
*/

with shipments as (
    select * from {{ ref('stg_logisys__shipments') }}
),

carriers as (
    select carrier_key, carrier_name, carrier_mode
    from {{ ref('dim_carrier') }}
),

customers as (
    select customer_key, customer_name, payment_terms_days
    from {{ ref('dim_customer') }}
)

select
    s.*,
    cr.carrier_name,
    cr.carrier_mode,
    cu.customer_name,
    cu.payment_terms_days,

    -- §6.1: on time = delivered on or before the committed date. NULL while
    -- undelivered — undelivered shipments are excluded from OTD, and counted
    -- separately as "overdue in transit" when past their committed date.
    case
        when s.actual_delivery_date is not null
            then s.actual_delivery_date <= s.committed_delivery_date
    end as is_on_time,

    -- §6.5: positive = late vs commitment, negative = early
    s.actual_transit_days - s.committed_transit_days as transit_variance_days,

    s.exception_count > 0 as has_exception

from shipments s
left join carriers cr on s.carrier_key = cr.carrier_key
left join customers cu on s.customer_key = cu.customer_key
