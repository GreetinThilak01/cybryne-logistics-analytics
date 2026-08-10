/*
    int_invoice_settlement
    ----------------------
    Produces one authoritative row per invoice for AR and ageing (§6.9).
    Pre-computes outstanding_inr and aging_bucket in SQL so Power BI never
    nets payments against invoices at query time — a common DAX failure mode
    for AR models.

    Credit notes are netted into the invoice they credit (target parsed from
    the voucher narration, e.g. "Credit note against MUM/24-25/0463") and do
    not appear as settlement rows of their own: a credit note is not a
    receivable. Ageing is measured in days past DUE date, not invoice date,
    as of {{ var('as_of_date') }} (CURRENT_DATE in production).
*/

with lines as (
    select * from {{ ref('int_invoices_matched') }}
),

-- credit notes: extract the target invoice from the narration and total them
credit_notes as (
    select
        regexp_extract(line_description, 'against (.+)$', 1) as target_invoice_number,
        sum(total_amount_inr) as cn_total_inr
    from lines
    where invoice_type = 'Credit Note'
    group by 1
),

-- receivable invoices (Original + Supplementary) at invoice grain
invoices as (
    select
        invoice_number,
        min(customer_key)       as customer_key,
        min(branch_key)         as branch_key,
        min(invoice_date)       as invoice_date,
        min(due_date)           as due_date,
        sum(total_amount_inr)   as gross_total_inr
    from lines
    where invoice_type != 'Credit Note'
    group by 1
),

payments as (
    select
        invoice_number,
        sum(amount_inr)         as amount_received_inr,
        max(payment_date)       as last_payment_date
    from {{ ref('stg_tally__payments') }}
    group by 1
),

settled as (
    select
        i.invoice_number,
        i.customer_key,
        i.branch_key,
        i.invoice_date,
        i.due_date,
        round(i.gross_total_inr + coalesce(cn.cn_total_inr, 0), 2) as invoice_total_inr,
        coalesce(p.amount_received_inr, 0)                         as amount_received_inr,
        p.last_payment_date
    from invoices i
    left join credit_notes cn on i.invoice_number = cn.target_invoice_number
    left join payments p on i.invoice_number = p.invoice_number
)

select
    invoice_number,
    customer_key,
    branch_key,
    invoice_date,
    due_date,
    invoice_total_inr,
    amount_received_inr,
    greatest(round(invoice_total_inr - amount_received_inr, 2), 0.00) as outstanding_inr,
    last_payment_date,
    case
        when round(invoice_total_inr - amount_received_inr, 2) <= 0.01 then 0
        else greatest(date_diff('day', due_date, cast('{{ var("as_of_date") }}' as date)), 0)
    end as days_overdue,
    case
        when round(invoice_total_inr - amount_received_inr, 2) <= 0.01 then 'Settled'
        when due_date >= cast('{{ var("as_of_date") }}' as date) then 'Current'
        when date_diff('day', due_date, cast('{{ var("as_of_date") }}' as date)) <= 30 then '0-30'
        when date_diff('day', due_date, cast('{{ var("as_of_date") }}' as date)) <= 60 then '31-60'
        when date_diff('day', due_date, cast('{{ var("as_of_date") }}' as date)) <= 90 then '61-90'
        else '90+'
    end as aging_bucket,
    round(invoice_total_inr - amount_received_inr, 2) <= 0.01 as is_settled

from settled
