/*
    assert_case_study_outcomes
    --------------------------
    Runs the full §7.8 assertion suite from the blueprint against the
    dbt-built mart. Returns ZERO ROWS when every case-study outcome is
    reproduced within tolerance; any failure returns one row with
    assertion_name, target, actual, difference — and fails `dbt test`.

    Cohorts are booking-date based: H1 = pre-implementation baseline,
    Q3–Q4 = post carrier-reallocation, Q4 = post-action run-rate for the
    margin uplift (Trident's Sep–Nov exit taper means the H2 *average*
    necessarily lags the end state the case study reports).

    A10 note: the pre-implementation reporting lag is reconstructed
    deterministically — completion + 1 reconciliation day, surfaced in the
    next Monday report, plus the expected value of skipped weeks
    (report_skip_rate * 7 days). The generator simulates the skips
    stochastically; this test uses their expectation.
*/

with shipments as (
    select
        s.*,
        m.mode,
        cr.carrier_code
    from {{ ref('fact_shipments') }} s
    left join {{ ref('dim_mode') }} m on s.mode_key = m.mode_key
    left join {{ ref('dim_carrier') }} cr on s.carrier_key = cr.carrier_key
),

problem_customers as (
    -- the two structurally unprofitable accounts from the case study
    select customer_key
    from {{ ref('dim_customer') }}
    where customer_code in ('CUST-ARHT', 'CUST-TRPL')
),

financials as (
    select
        f.*,
        s.booking_date,
        f.customer_key in (select customer_key from problem_customers) as is_problem
    from {{ ref('fact_shipment_financials') }} f
    join {{ ref('fact_shipments') }} s on f.shipment_key = s.shipment_key
    where f.is_fully_billed
),

go_live as (select cast('{{ var("go_live_date") }}' as date) as d),

-- ---------------------------------------------------------------- metrics --
m_counts as (
    select
        count(*)                                        as total_shipments,
        avg(case when mode = 'Ocean' then 1.0 else 0 end) as ocean_share
    from shipments
),

m_otd as (
    select
        avg(case when booking_date <  (select d from go_live) and actual_delivery_date is not null
                 then cast(is_on_time as int) end) as otd_h1,
        avg(case when booking_date >= (select d from go_live) and actual_delivery_date is not null
                 then cast(is_on_time as int) end) as otd_h2,
        avg(case when carrier_code = 'ASW' and actual_delivery_date is not null
                 then cast(is_on_time as int) end) as otd_asw
    from shipments
),

m_carrier_otd as (
    select carrier_code, avg(cast(is_on_time as int)) as otd
    from shipments
    where actual_delivery_date is not null and carrier_code != 'ASW'
    group by 1
),

m_volume_share as (
    select avg(case when customer_key in (select customer_key from problem_customers)
                    then 1.0 else 0 end) as problem_share
    from shipments
    where booking_date < (select d from go_live)
),

m_margin_share as (
    select
        sum(case when is_problem then gross_margin_inr else 0 end)
            / sum(gross_margin_inr) as problem_gm_share
    from financials
    where booking_date < (select d from go_live)
),

m_billing_lag as (
    select avg(billing_lag_days) as mean_lag
    from {{ ref('fact_shipment_financials') }}
    where billing_lag_days is not null
),

m_reporting_lag as (
    select
        avg(date_diff(
                'day',
                job_completion_date,
                (job_completion_date + 1)
                    + cast((8 - isodow(job_completion_date + 1)) % 7 as integer)
            ))
        + {{ var('report_skip_rate') }} * 7 as mean_reporting_lag
    from shipments
    where job_completion_date < (select d from go_live)
),

m_margin_uplift as (
    select
        sum(case when booking_date >= date '2024-10-01' then gross_margin_inr end)
            / sum(case when booking_date >= date '2024-10-01' then revenue_billed_inr end)
      - sum(case when booking_date < (select d from go_live) then gross_margin_inr end)
            / sum(case when booking_date < (select d from go_live) then revenue_billed_inr end)
        as gm_uplift
    from financials
),

m_capture as (
    select
        sum(case when is_problem then surcharge_billed_inr end)
            / sum(case when is_problem then surcharge_cost_inr end) as capture_problem,
        sum(case when not is_problem then surcharge_billed_inr end)
            / sum(case when not is_problem then surcharge_cost_inr end) as capture_normal
    from financials
    where booking_date < (select d from go_live) and surcharge_cost_inr > 0
),

m_integrity as (
    select
        (select count(*) from {{ ref('fact_charges') }} c
          left join {{ ref('fact_shipments') }} s on c.shipment_key = s.shipment_key
          where s.shipment_key is null)
      + (select count(*) from {{ ref('fact_exceptions') }} e
          left join {{ ref('fact_shipments') }} s on e.shipment_key = s.shipment_key
          where s.shipment_key is null)
      + (select count(*) from {{ ref('fact_payments') }} p
          left join {{ ref('fact_invoice_settlement') }} st on p.invoice_number = st.invoice_number
          where st.invoice_number is null) as dangling_fks,
        (select avg(case when shipment_key is null then 1.0 else 0 end)
           from {{ ref('fact_invoice_lines') }}) as unmatched_rate
),

-- ------------------------------------------------------------- assertions --
failures as (

    select 'A1 total shipments' as assertion_name,
           '[4500, 4620]' as target,
           cast(total_shipments as varchar) as actual,
           cast(total_shipments - 4560 as varchar) as difference
    from m_counts where total_shipments not between 4500 and 4620

    union all
    select 'A2 ocean share', '[0.635, 0.665]',
           cast(round(ocean_share, 4) as varchar),
           cast(round(ocean_share - 0.65, 4) as varchar)
    from m_counts where ocean_share not between 0.635 and 0.665

    union all
    select 'A3 H1 overall OTD', '[0.80, 0.82]',
           cast(round(otd_h1, 4) as varchar),
           cast(round(otd_h1 - 0.81, 4) as varchar)
    from m_otd where otd_h1 not between 0.80 and 0.82

    union all
    select 'A4 Q3-Q4 overall OTD', '[0.87, 0.89]',
           cast(round(otd_h2, 4) as varchar),
           cast(round(otd_h2 - 0.88, 4) as varchar)
    from m_otd where otd_h2 not between 0.87 and 0.89

    union all
    select 'A5 AeroSwift 12-mo OTD', '[0.65, 0.69]',
           cast(round(otd_asw, 4) as varchar),
           cast(round(otd_asw - 0.67, 4) as varchar)
    from m_otd where otd_asw not between 0.65 and 0.69

    union all
    select 'A6 carrier OTD band: ' || carrier_code, '[0.85, 0.92]',
           cast(round(otd, 4) as varchar),
           cast(round(case when otd < 0.85 then otd - 0.85 else otd - 0.92 end, 4) as varchar)
    from m_carrier_otd where otd not between 0.85 and 0.92

    union all
    select 'A7 problem customers H1 volume share', '[0.21, 0.23]',
           cast(round(problem_share, 4) as varchar),
           cast(round(problem_share - 0.22, 4) as varchar)
    from m_volume_share where problem_share not between 0.21 and 0.23

    union all
    select 'A8 problem customers H1 margin share', '[0.00, 0.04)',
           cast(round(problem_gm_share, 4) as varchar),
           cast(round(problem_gm_share - 0.04, 4) as varchar)
    from m_margin_share where problem_gm_share < 0 or problem_gm_share >= 0.04

    union all
    select 'A9 mean billing lag (days)', '[7, 10]',
           cast(round(mean_lag, 2) as varchar),
           cast(round(mean_lag - 8.5, 2) as varchar)
    from m_billing_lag where mean_lag not between 7 and 10

    union all
    select 'A10 pre-implementation reporting lag (days)', '[4.4, 4.8]',
           cast(round(mean_reporting_lag, 2) as varchar),
           cast(round(mean_reporting_lag - 4.6, 2) as varchar)
    from m_reporting_lag where mean_reporting_lag not between 4.4 and 4.8

    union all
    select 'A11 blended GM% uplift Q4 vs H1', '[0.028, 0.036]',
           cast(round(gm_uplift, 4) as varchar),
           cast(round(gm_uplift - 0.032, 4) as varchar)
    from m_margin_uplift where gm_uplift not between 0.028 and 0.036

    union all
    select 'A12 surcharge capture: problem customers', '[0.22, 0.37]',
           cast(round(capture_problem, 4) as varchar),
           cast(round(capture_problem - 0.32, 4) as varchar)
    from m_capture where capture_problem not between 0.22 and 0.37

    union all
    select 'A12 surcharge capture: normal customers', '[0.83, 0.94]',
           cast(round(capture_normal, 4) as varchar),
           cast(round(capture_normal - 0.88, 4) as varchar)
    from m_capture where capture_normal not between 0.83 and 0.94

    union all
    select 'A13 dangling foreign keys', '0',
           cast(dangling_fks as varchar),
           cast(dangling_fks as varchar)
    from m_integrity where dangling_fks != 0

    union all
    select 'A13 unmatched invoice-line rate', '[0.005, 0.025]',
           cast(round(unmatched_rate, 4) as varchar),
           cast(round(unmatched_rate - 0.015, 4) as varchar)
    from m_integrity where unmatched_rate not between 0.005 and 0.025
)

select * from failures
