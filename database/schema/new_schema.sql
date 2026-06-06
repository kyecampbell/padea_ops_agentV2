-- =============================================================================
-- PADEA Operations Agent — NEW Database Schema (rebuilt for a fresh Supabase DB)
-- =============================================================================
--
-- Guiding principle: the orchestrator must never be blocked by structure.
-- Anything that can grow in cardinality is representable by INSERT/UPDATE,
-- never ALTER. Hence: every ENUM becomes a reference (lookup) table; fixed
-- MOQ tier columns and the fixed feedback checklist become rows.
--
-- Conventions (Part 4 of the spec):
--   * PostgreSQL / Supabase.
--   * Money is always integer CENTS.
--   * All timestamps are timestamptz.
--   * bigserial / bigint PKs, FK style carried over from the old schema.
--   * updated_at auto-maintained by trigger (the OPT-01 pattern), on the same
--     six core tables that had it before.
--   * Reference tables (the enum replacements) are seeded inline with the old
--     enum values so nothing is lost. Business seed data is a SEPARATE step.
--
-- Run order: trigger fn -> reference tables (+seed) -> core tables -> indexes
--            -> triggers. Runnable top-to-bottom in a clean project.
-- =============================================================================

SET client_min_messages = warning;


-- =============================================================================
-- 0. SHARED TRIGGER FUNCTION (OPT-01)
-- =============================================================================

CREATE OR REPLACE FUNCTION public.set_updated_at() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN NEW.updated_at = now(); RETURN NEW; END;
$$;


-- =============================================================================
-- 1. REFERENCE (LOOKUP) TABLES — replace the eight old ENUM types
-- =============================================================================
-- Shape: (code text PK, label, description, active, sort_order). A new value is
-- an INSERT, never DDL. Each FK column below is `text REFERENCES <table>(code)`.
-- Seeded inline with the OLD enum's current values.
-- NOTE: the old `order_rotation_status` enum is intentionally NOT recreated —
-- its only consumer, orders.rotation_status, is dropped in this rebuild
-- (T-72hr per-session rotation -> one weekly Thursday batch). See summary.

CREATE TABLE public.email_type (
    code        text    PRIMARY KEY,
    label       text    NOT NULL,
    description text,
    active      boolean NOT NULL DEFAULT true,
    sort_order  integer NOT NULL DEFAULT 0
);
COMMENT ON TABLE public.email_type IS 'Lookup: kind of outbound email. Replaces enum email_type. Agent may INSERT new kinds.';

CREATE TABLE public.email_status (
    code        text    PRIMARY KEY,
    label       text    NOT NULL,
    description text,
    active      boolean NOT NULL DEFAULT true,
    sort_order  integer NOT NULL DEFAULT 0
);
COMMENT ON TABLE public.email_status IS 'Lookup: lifecycle status of an outbound email. Replaces enum email_status.';

CREATE TABLE public.step_urgency (
    code        text    PRIMARY KEY,
    label       text    NOT NULL,
    description text,
    active      boolean NOT NULL DEFAULT true,
    sort_order  integer NOT NULL DEFAULT 0
);
COMMENT ON TABLE public.step_urgency IS 'Lookup: decision-feed severity of an agent step. Replaces enum step_urgency. A new tier (e.g. "blocking") is one INSERT.';

CREATE TABLE public.feedback_source (
    code        text    PRIMARY KEY,
    label       text    NOT NULL,
    description text,
    active      boolean NOT NULL DEFAULT true,
    sort_order  integer NOT NULL DEFAULT 0
);
COMMENT ON TABLE public.feedback_source IS 'Lookup: who supplied a feedback row. Replaces enum feedback_source (parent/student can be added later).';

CREATE TABLE public.inbound_classification (
    code        text    PRIMARY KEY,
    label       text    NOT NULL,
    description text,
    active      boolean NOT NULL DEFAULT true,
    sort_order  integer NOT NULL DEFAULT 0
);
COMMENT ON TABLE public.inbound_classification IS 'Lookup: classification of an inbound email. Replaces enum inbound_classification. Agent meets novel kinds -> INSERT.';

CREATE TABLE public.order_line_source (
    code        text    PRIMARY KEY,
    label       text    NOT NULL,
    description text,
    active      boolean NOT NULL DEFAULT true,
    sort_order  integer NOT NULL DEFAULT 0
);
COMMENT ON TABLE public.order_line_source IS 'Lookup: provenance of an order line. Replaces enum order_line_source.';

CREATE TABLE public.preference_capture_source (
    code        text    PRIMARY KEY,
    label       text    NOT NULL,
    description text,
    active      boolean NOT NULL DEFAULT true,
    sort_order  integer NOT NULL DEFAULT 0
);
COMMENT ON TABLE public.preference_capture_source IS 'Lookup: who captured a term meal preference. Replaces enum preference_capture_source.';

-- New reference tables (no old enum) for the spec''s new agent_steps / escalations
-- columns. Seeded with sensible defaults; flagged in the summary as NEW.
CREATE TABLE public.action_class (
    code        text    PRIMARY KEY,
    label       text    NOT NULL,
    description text,
    active      boolean NOT NULL DEFAULT true,
    sort_order  integer NOT NULL DEFAULT 0
);
COMMENT ON TABLE public.action_class IS 'Lookup (NEW): whether an agent step is autonomous or requires approval. Backs agent_steps.action_class.';

CREATE TABLE public.escalation_status (
    code        text    PRIMARY KEY,
    label       text    NOT NULL,
    description text,
    active      boolean NOT NULL DEFAULT true,
    sort_order  integer NOT NULL DEFAULT 0
);
COMMENT ON TABLE public.escalation_status IS 'Lookup (NEW): lifecycle status of an escalation. Backs escalations.status.';


-- --- Seed reference tables (old enum values preserved) -----------------------

INSERT INTO public.email_type (code, label, description, sort_order) VALUES
    ('session_order',                 'Session order',                 'Per-session order to caterer (binding)',                    10),
    ('weekly_consolidated_summary',   'Weekly consolidated summary',   'Per-caterer Monday consolidated summary',                   20),
    ('caterer_weekly_summary',        'Caterer weekly summary',        'Warm Monday per-caterer quality scorecard: praise + per-school student satisfaction + recurring themes + gentle service note + capacity ask; autonomous', 25),
    ('warning',                       'Warning',                       'Decline-detection warning to incumbent caterer',            30),
    ('rfp',                           'RFP',                           'Request for proposal to candidate caterers',                40),
    ('cancellation',                  'Cancellation',                  'Cancellation notice to outgoing caterer',                   50),
    ('rfp_loser_courtesy',            'RFP loser courtesy',            'Thanks-but-no-thanks to unsuccessful RFP candidates',       60),
    ('parent_enrolment',              'Parent enrolment',              'Term-start enrolment email to parent',                      70),
    ('parent_reminder',               'Parent reminder',               'Chase reminder to parent',                                  80),
    ('parent_prefs_request',          'Parent preferences request',    'One-time request to a parent for the student''s meal preferences', 85),
    ('student_meal_choice',           'Student meal choice',           'Weekly choose-and-rate email to a student: rate last week + pick this week from MOQ-bounded, dietary-safe options; autonomous', 87),
    ('opt_back_in_request_to_parent', 'Opt back-in request to parent', 'Tutor-triggered opt-back-in request (autonomous)',          90),
    ('operator_notification',         'Operator notification',         'System-to-operator escalation notice',                     100),
    ('other',                         'Other',                         'Uncategorised',                                            110)
ON CONFLICT (code) DO NOTHING;

INSERT INTO public.email_status (code, label, description, sort_order) VALUES
    ('drafted',              'Drafted',              'Composed, not yet routed',                          10),
    ('queued_for_approval',  'Queued for approval',  'Commercial email awaiting operator approval',       20),
    ('approved',             'Approved',             'Operator approved; ready to send',                  30),
    ('sending',              'Sending',              'Send in progress',                                  40),
    ('sent',                 'Sent',                 'Successfully sent via Gmail',                       50),
    ('failed',               'Failed',               'Send attempt failed',                               60),
    ('rejected',             'Rejected',             'Operator rejected the queued draft; superseded by a feedback re-run', 70)
ON CONFLICT (code) DO NOTHING;

INSERT INTO public.step_urgency (code, label, description, sort_order) VALUES
    ('urgent',        'Urgent',        'Gating something; needs attention now',  10),
    ('notable',       'Notable',       'Pattern worth investigating',            20),
    ('informational', 'Informational', 'Audit trail',                            30),
    ('none',          'None',          'Routine tool call',                      40)
ON CONFLICT (code) DO NOTHING;

INSERT INTO public.feedback_source (code, label, description, sort_order) VALUES
    ('tutor',   'Tutor',   'Per-meal feedback from the session tutor',     10),
    ('manager', 'Manager', 'Per-order feedback from the session manager',  20),
    ('student', 'Student', 'Per-meal rating + comment submitted by the student in the weekly choose-and-rate email', 30)
ON CONFLICT (code) DO NOTHING;

INSERT INTO public.inbound_classification (code, label, description, sort_order) VALUES
    ('absence',                            'Absence',                            'Parent-notified student absence',           10),
    ('caterer_order_confirmation',         'Caterer order confirmation',         'Caterer confirming an order',               20),
    ('caterer_price_change_notification',  'Caterer price change notification',  'Caterer notifying a price change',          30),
    ('parent_enrolment_response',          'Parent enrolment response',          'Parent responding to an enrolment email',   40),
    ('student_meal_choice',                'Student meal choice',                'Student replying to the weekly choose-and-rate email with their meal pick + rating', 45),
    ('unclassified',                       'Unclassified',                       'Could not be classified',                   50)
ON CONFLICT (code) DO NOTHING;

INSERT INTO public.order_line_source (code, label, description, sort_order) VALUES
    ('rotation',              'Rotation',              'Drawn from the standing preference set',             10),
    ('request',               'Request',               'One-off per-session meal request',                   20),
    ('defaulted_pending_confirmation', 'Defaulted (pending confirmation)',
     'Safe default for a student with no usable preference; parent to confirm within 48h',                  30),
    ('dietary_auto_pick',     'Dietary auto-pick',     'Auto-selected to satisfy dietary safety',            40)
ON CONFLICT (code) DO NOTHING;

INSERT INTO public.preference_capture_source (code, label, description, sort_order) VALUES
    ('parent',   'Parent',   'Captured from the parent at term start',     10),
    ('tutor',    'Tutor',    'Captured by the tutor on caterer change',    20),
    ('operator', 'Operator', 'Captured by the operator mid-term',          30)
ON CONFLICT (code) DO NOTHING;

-- NEW reference values (no old enum) — sensible defaults; extend by INSERT.
INSERT INTO public.action_class (code, label, description, sort_order) VALUES
    ('autonomous',        'Autonomous',        'Agent may act without operator approval',  10),
    ('requires_approval', 'Requires approval', 'Action must be approved before it fires',  20)
ON CONFLICT (code) DO NOTHING;

INSERT INTO public.escalation_status (code, label, description, sort_order) VALUES
    ('open',      'Open',      'Raised, awaiting resolution',         10),
    ('resolved',  'Resolved',  'Resolved by the operator',            20),
    ('dismissed', 'Dismissed', 'Closed without action',               30)
ON CONFLICT (code) DO NOTHING;


-- =============================================================================
-- 2. CORE TABLES
-- =============================================================================
-- Ordered so every FK target is created before it is referenced.

-- --- Caterers (referenced by schools, menu_items, orders, feedback, ...) -----
CREATE TABLE public.caterers (
    id                  bigserial       PRIMARY KEY,
    name                text            NOT NULL,
    contact_email       text            NOT NULL,
    contact_phone       text,
    home_postcode       text            NOT NULL,
    max_delivery_km     integer         NOT NULL DEFAULT 50,
    delivery_fee_cents  integer         NOT NULL DEFAULT 0,
    price_includes_gst  boolean         NOT NULL DEFAULT false,
    gst_rate_percent    numeric(4,2)    NOT NULL DEFAULT 10.0,
    created_at          timestamptz     NOT NULL DEFAULT now(),
    updated_at          timestamptz     NOT NULL DEFAULT now()
    -- DROPPED vs old: moq_4/5/6_items -> caterer_moq_tier rows;
    --                 canonical_menu_order + canonical_order_set_at (vestigial).
);
COMMENT ON TABLE public.caterers IS 'Catering suppliers. MOQ tiers now live in caterer_moq_tier. V3 canonical_menu_order popularity snapshot dropped.';

-- --- Schools -----------------------------------------------------------------
CREATE TABLE public.schools (
    id                  bigserial       PRIMARY KEY,
    name                text            NOT NULL,
    building            text,
    postcode            text            NOT NULL,
    current_caterer_id  bigint          REFERENCES public.caterers(id),
    created_at          timestamptz     NOT NULL DEFAULT now(),
    updated_at          timestamptz     NOT NULL DEFAULT now()
);
COMMENT ON TABLE public.schools IS 'Padea school locations. One caterer per school (structural assumption, retained).';

-- --- Caterer MOQ tiers (replaces moq_4/5/6_items fixed columns) --------------
CREATE TABLE public.caterer_moq_tier (
    caterer_id      bigint   NOT NULL REFERENCES public.caterers(id),
    variety_count   integer  NOT NULL,
    min_total_items integer  NOT NULL,
    PRIMARY KEY (caterer_id, variety_count)
);
COMMENT ON TABLE public.caterer_moq_tier IS 'Minimum-order tiers per caterer: at variety_count distinct items, the floor is min_total_items meals. New tier = one INSERT (was moq_4/5/6_items columns).';

-- --- Menu items --------------------------------------------------------------
CREATE TABLE public.menu_items (
    id              bigserial       PRIMARY KEY,
    caterer_id      bigint          NOT NULL REFERENCES public.caterers(id),
    name            text            NOT NULL,
    description     text,
    contents_text   text,
    tweaks_text     text,
    price_cents     integer         NOT NULL,
    active          boolean         NOT NULL DEFAULT true,
    created_at      timestamptz     NOT NULL DEFAULT now(),
    updated_at      timestamptz     NOT NULL DEFAULT now()
);
COMMENT ON TABLE public.menu_items IS 'Per-caterer menu. contents_text = dietary-relevant ingredients; tweaks_text = safe modifications (e.g. vegetarian option). Dietary properties via menu_item_dietary_tags.';

-- --- Tutors ------------------------------------------------------------------
CREATE TABLE public.tutors (
    id                  bigserial       PRIMARY KEY,
    name                text            NOT NULL,
    email               text,
    mobile              text,
    employee_identifier text,
    active              boolean         NOT NULL DEFAULT true,
    created_at          timestamptz     NOT NULL DEFAULT now(),
    updated_at          timestamptz     NOT NULL DEFAULT now()
);
COMMENT ON TABLE public.tutors IS 'Tutors (and, via session_tutor_assignments flags, session managers).';

-- --- Dietary vocabulary ------------------------------------------------------
CREATE TABLE public.dietary_tags (
    id          bigserial       PRIMARY KEY,
    name        text            NOT NULL UNIQUE,
    label       text            NOT NULL,
    description text,
    active      boolean         NOT NULL DEFAULT true,
    created_at  timestamptz     NOT NULL DEFAULT now()
);
COMMENT ON TABLE public.dietary_tags IS 'Extensible dietary vocabulary. On a student: "I require this." On an item: "I satisfy this." Safety: item tags must contain student tags. Model may INSERT new tags.';

-- --- Enrolments --------------------------------------------------------------
CREATE TABLE public.enrolments (
    id                          bigserial       PRIMARY KEY,
    school_id                   bigint          NOT NULL REFERENCES public.schools(id),
    student_name                text            NOT NULL,
    student_year_level          integer,
    parent_name                 text            NOT NULL,
    parent_email                text            NOT NULL,
    parent_phone                text,
    student_email               text,
    original_start_date         date            NOT NULL,
    current_period_start_date   date            NOT NULL,
    current_period_end_date     date,
    opted_out_of_catering       boolean         NOT NULL DEFAULT false,
    dietary_raw                 text,
    created_at                  timestamptz     NOT NULL DEFAULT now(),
    updated_at                  timestamptz     NOT NULL DEFAULT now()
);
COMMENT ON TABLE public.enrolments IS 'Student-at-school with denormalised student+parent identity and three-date lifecycle. dietary_raw = the verbatim dietary string; tags are derived from it into enrolment_dietary_tags.';
COMMENT ON COLUMN public.enrolments.student_email IS 'The student''s own contact for the weekly choose-and-rate email. NULL/blank = not emailed; the student falls back to compose-time assignment.';

CREATE TABLE public.enrolment_dietary_tags (
    enrolment_id    bigint          NOT NULL REFERENCES public.enrolments(id),
    dietary_tag_id  bigint          NOT NULL REFERENCES public.dietary_tags(id),
    captured_at     timestamptz     NOT NULL DEFAULT now(),
    PRIMARY KEY (enrolment_id, dietary_tag_id)
);
COMMENT ON TABLE public.enrolment_dietary_tags IS 'Junction: dietary properties a student requires.';

CREATE TABLE public.menu_item_dietary_tags (
    menu_item_id    bigint          NOT NULL REFERENCES public.menu_items(id),
    dietary_tag_id  bigint          NOT NULL REFERENCES public.dietary_tags(id),
    PRIMARY KEY (menu_item_id, dietary_tag_id)
);
COMMENT ON TABLE public.menu_item_dietary_tags IS 'Junction: dietary properties a menu item satisfies. Safety rule: item tags must contain student tags (set containment).';

-- --- Session operations ------------------------------------------------------
CREATE TABLE public.session_slots (
    id              bigserial       PRIMARY KEY,
    school_id       bigint          NOT NULL REFERENCES public.schools(id),
    day_of_week     integer         NOT NULL,
    start_time      time            NOT NULL,
    dinner_time     time            NOT NULL,
    end_time        time            NOT NULL,
    room            text,
    active          boolean         NOT NULL DEFAULT true,
    created_at      timestamptz     NOT NULL DEFAULT now(),
    updated_at      timestamptz     NOT NULL DEFAULT now(),
    UNIQUE (school_id, day_of_week)
);
COMMENT ON TABLE public.session_slots IS 'Recurring (school, day-of-week) sessions. day_of_week: 1=Mon..7=Sun.';

CREATE TABLE public.session_tutor_assignments (
    id              bigserial       PRIMARY KEY,
    session_slot_id bigint          NOT NULL REFERENCES public.session_slots(id),
    session_date    date            NOT NULL,
    tutor_id        bigint          NOT NULL REFERENCES public.tutors(id),
    is_manager      boolean         NOT NULL DEFAULT false,
    is_tutor        boolean         NOT NULL DEFAULT false,
    assigned_at     timestamptz     NOT NULL DEFAULT now(),
    UNIQUE (session_slot_id, session_date, tutor_id)
);
COMMENT ON TABLE public.session_tutor_assignments IS 'Per-session tutor/manager assignment. Dual-role booleans cover all four manager/tutor combinations.';

-- --- Per-student session roster ----------------------------------------------
-- Which session_slot(s) at their school a student attends. A student's meals are
-- composed from their ROSTERED sessions only (not every session at the school),
-- and each session's cohort is the set of students rostered to it. A student may
-- be rostered to more than one session (PK allows it).
CREATE TABLE public.enrolment_session_slots (
    enrolment_id    bigint  NOT NULL REFERENCES public.enrolments(id),
    session_slot_id bigint  NOT NULL REFERENCES public.session_slots(id),
    PRIMARY KEY (enrolment_id, session_slot_id)
);
COMMENT ON TABLE public.enrolment_session_slots IS 'Per-student session roster: the session_slot(s) at their school a student attends. A student''s meals are composed from these sessions only; each session''s cohort is the students rostered to it. Generated deterministically (keyed on enrolment id) by scripts/build_session_rosters.py.';

CREATE TABLE public.exclusions (
    id              bigserial       PRIMARY KEY,
    school_id       bigint          REFERENCES public.schools(id),
    enrolment_id    bigint          REFERENCES public.enrolments(id),
    start_date      date            NOT NULL,
    end_date        date            NOT NULL,
    reason          text,
    created_at      timestamptz     NOT NULL DEFAULT now()
);
COMMENT ON TABLE public.exclusions IS 'Date-range exclusions. school_id NULL = system-wide; enrolment_id NULL = full-school (e.g. holidays).';

CREATE TABLE public.absences (
    id                          bigserial       PRIMARY KEY,
    enrolment_id                bigint          NOT NULL REFERENCES public.enrolments(id),
    absence_date                date            NOT NULL,
    received_at                 timestamptz     NOT NULL DEFAULT now(),
    source_email_filename       text,
    source_email_message_id     text,
    notes                       text,
    UNIQUE (enrolment_id, absence_date)
);
COMMENT ON TABLE public.absences IS 'Parent-notified absences, deduped per (enrolment, date).';

-- --- Preferences + requests --------------------------------------------------
CREATE TABLE public.term_meal_preferences (
    id              bigserial       PRIMARY KEY,
    enrolment_id    bigint          NOT NULL REFERENCES public.enrolments(id),
    caterer_id      bigint          NOT NULL REFERENCES public.caterers(id),
    captured_at     timestamptz     NOT NULL DEFAULT now(),
    captured_by     text            NOT NULL REFERENCES public.preference_capture_source(code),
    superseded_at   timestamptz,
    notes           text
);
COMMENT ON TABLE public.term_meal_preferences IS 'Per (enrolment, caterer) approved meal set. Supersession-aware: old sets archive on caterer swap.';

CREATE TABLE public.term_meal_preference_items (
    preference_id   bigint          NOT NULL REFERENCES public.term_meal_preferences(id),
    menu_item_id    bigint          NOT NULL REFERENCES public.menu_items(id),
    rank            integer,
    PRIMARY KEY (preference_id, menu_item_id)
);
COMMENT ON TABLE public.term_meal_preference_items IS 'Junction: menu items in a preference set. rank = ordered preference (1 = top); top available item is the standing meal. Minimum K enforced in the app layer.';

CREATE TABLE public.meal_requests (
    id              bigserial       PRIMARY KEY,
    enrolment_id    bigint          NOT NULL REFERENCES public.enrolments(id),
    session_slot_id bigint          NOT NULL REFERENCES public.session_slots(id),
    session_date    date            NOT NULL,
    menu_item_id    bigint          NOT NULL REFERENCES public.menu_items(id),
    requested_at    timestamptz     NOT NULL DEFAULT now(),
    consumed_at     timestamptz,
    UNIQUE (enrolment_id, session_slot_id, session_date)
);
COMMENT ON TABLE public.meal_requests IS 'Per-kid per-session one-off meal override, drawn from the full dietary-safe menu.';

-- --- Orders ------------------------------------------------------------------
CREATE TABLE public.orders (
    id                      bigserial       PRIMARY KEY,
    session_slot_id         bigint          NOT NULL REFERENCES public.session_slots(id),
    caterer_id              bigint          NOT NULL REFERENCES public.caterers(id),
    session_date            date            NOT NULL,
    total_items             integer         NOT NULL,
    total_cost_cents        integer         NOT NULL,
    gst_rate_percent        numeric(5,2)    NOT NULL,
    composed_at             timestamptz     NOT NULL DEFAULT now(),
    sent_at                 timestamptz,
    UNIQUE (session_slot_id, session_date)
    -- DROPPED vs old: is_preview_week, rotation_status (T-72hr leftovers); and
    --                 moq_floor_applied / moq_variance_cents -> caterer_week_orders
    --                 (MOQ is now a per-caterer-per-week measure, not per session).
);
COMMENT ON TABLE public.orders IS 'Per-session order (meal lines + per-session cost snapshot). Zero operational margin. MOQ + weekly finance live in caterer_week_orders.';

CREATE TABLE public.order_lines (
    id              bigserial       PRIMARY KEY,
    order_id        bigint          NOT NULL REFERENCES public.orders(id),
    enrolment_id    bigint          NOT NULL REFERENCES public.enrolments(id),
    menu_item_id    bigint          NOT NULL REFERENCES public.menu_items(id),
    source          text            NOT NULL REFERENCES public.order_line_source(code),
    UNIQUE (order_id, enrolment_id)
);
COMMENT ON TABLE public.order_lines IS 'Per-meal-per-kid order rows. Required so feedback can attach at meal granularity.';

-- --- Derived dietary-safe pool (NEW) -----------------------------------------
CREATE TABLE public.student_eligible_meals (
    enrolment_id    bigint          NOT NULL REFERENCES public.enrolments(id),
    menu_item_id    bigint          NOT NULL REFERENCES public.menu_items(id),
    eligible        boolean         NOT NULL,
    needs_tweak     boolean         NOT NULL DEFAULT false,
    rationale       text,
    computed_at     timestamptz     NOT NULL DEFAULT now(),
    PRIMARY KEY (enrolment_id, menu_item_id)
);
COMMENT ON TABLE public.student_eligible_meals IS 'Derived dietary-safe pool, recomputable when a student dietary, a menu item, or its tweaks change. needs_tweak = eligible only as e.g. vegetarian option. rationale feeds the decision log.';

-- --- Feedback + checklist ----------------------------------------------------
CREATE TABLE public.feedback (
    id                  bigserial       PRIMARY KEY,
    source              text            NOT NULL REFERENCES public.feedback_source(code),
    order_line_id       bigint          REFERENCES public.order_lines(id),
    order_id            bigint          REFERENCES public.orders(id),
    tutor_id            bigint          REFERENCES public.tutors(id),
    caterer_id          bigint          NOT NULL REFERENCES public.caterers(id),
    rating              integer         CHECK (rating IS NULL OR rating BETWEEN 1 AND 5),
    comment             text,
    meals_left          integer,
    kids_who_didnt_eat  text,
    submitted_at        timestamptz     NOT NULL DEFAULT now()
    -- DROPPED vs old: the five fixed checklist booleans (food_on_time,
    --                 correct_count_received, correct_dietary_delivered,
    --                 food_temperature_ok, visibly_wrong) -> checklist_item +
    --                 feedback_checklist_response. meals_left / kids_who_didnt_eat
    --                 retained (free-form manager fields, not the checklist).
);
COMMENT ON TABLE public.feedback IS 'Polymorphic feedback. tutor -> order_line; manager -> order. Null rating = not filled in. caterer_id denormalised for rolling-mean hot path. Checklist answers now in feedback_checklist_response.';

CREATE TABLE public.checklist_item (
    id          bigserial       PRIMARY KEY,
    code        text            NOT NULL UNIQUE,
    prompt      text            NOT NULL,
    applies_to  text,
    active      boolean         NOT NULL DEFAULT true,
    sort_order  integer         NOT NULL DEFAULT 0,
    created_at  timestamptz     NOT NULL DEFAULT now()
);
COMMENT ON TABLE public.checklist_item IS 'Extensible quality checklist. A new question is a row (was fixed boolean columns on feedback). applies_to = e.g. manager/tutor. Legacy items seeded in the separate seed step.';

CREATE TABLE public.feedback_checklist_response (
    feedback_id         bigint          NOT NULL REFERENCES public.feedback(id),
    checklist_item_id   bigint          NOT NULL REFERENCES public.checklist_item(id),
    value_bool          boolean,
    value_text          text,
    PRIMARY KEY (feedback_id, checklist_item_id)
);
COMMENT ON TABLE public.feedback_checklist_response IS 'One answer per (feedback, checklist_item). value_bool covers the legacy boolean checklist; value_text supports future free-text questions.';

CREATE TABLE public.opt_back_in_requests (
    id                  bigserial       PRIMARY KEY,
    enrolment_id        bigint          NOT NULL REFERENCES public.enrolments(id),
    session_slot_id     bigint          NOT NULL REFERENCES public.session_slots(id),
    session_date        date            NOT NULL,
    submitted_by_tutor  bigint          REFERENCES public.tutors(id),
    submitted_at        timestamptz     NOT NULL DEFAULT now(),
    email_sent_at       timestamptz,
    parent_resolved_at  timestamptz
);
COMMENT ON TABLE public.opt_back_in_requests IS 'Tutor opt-back-in ticks. Send-once via email_sent_at: first tick per opt-out period fires the email; later ticks record only.';

-- --- Observability -----------------------------------------------------------
CREATE TABLE public.agent_runs (
    id              bigserial       PRIMARY KEY,
    started_at      timestamptz     NOT NULL DEFAULT now(),
    completed_at    timestamptz,
    trigger_reason  text            NOT NULL,
    notes           text,
    task            text,
    parent_run_id   bigint          REFERENCES public.agent_runs(id),
    feedback_depth  integer         NOT NULL DEFAULT 0
);
COMMENT ON TABLE public.agent_runs IS 'One row per agent invocation (e.g. session_order_t72, monday_summary, manual). task = the prompt (replayed by feedback re-runs); parent_run_id/feedback_depth track the bounded redo chain.';

CREATE TABLE public.agent_steps (
    id                  bigserial       PRIMARY KEY,
    run_id              bigint          NOT NULL REFERENCES public.agent_runs(id),
    step_index          integer         NOT NULL,
    tool_name           text,
    tool_input          jsonb,
    tool_output_full    jsonb,
    reasoning           text,
    urgency             text            NOT NULL DEFAULT 'none' REFERENCES public.step_urgency(code),
    confidence          numeric         CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    action_class        text            REFERENCES public.action_class(code),
    created_at          timestamptz     NOT NULL DEFAULT now(),
    UNIQUE (run_id, step_index)
);
COMMENT ON TABLE public.agent_steps IS 'Per-step audit. urgency drives the decision feed. confidence = agent self-assessment (0..1); action_class = autonomous vs requires-approval, so the gate decision is auditable.';

-- --- Communications ----------------------------------------------------------
CREATE TABLE public.outbound_emails (
    id                      bigserial       PRIMARY KEY,
    email_type              text            NOT NULL REFERENCES public.email_type(code),
    status                  text            NOT NULL DEFAULT 'drafted' REFERENCES public.email_status(code),
    intended_to_address     text            NOT NULL,
    intended_cc_addresses   jsonb,
    subject                 text            NOT NULL,
    rendered_body           text            NOT NULL,
    composed_at             timestamptz     NOT NULL DEFAULT now(),
    queued_for_approval_at  timestamptz,
    approved_at             timestamptz,
    approved_by             text,
    sent_at                 timestamptz,
    failed_at               timestamptz,
    failure_reason          text,
    gmail_message_id        text            UNIQUE,
    related_run_id          bigint          REFERENCES public.agent_runs(id),
    related_step_id         bigint          REFERENCES public.agent_steps(id),
    related_order_id        bigint          REFERENCES public.orders(id),
    related_caterer_id      bigint          REFERENCES public.caterers(id),
    related_enrolment_id    bigint          REFERENCES public.enrolments(id)
);
COMMENT ON TABLE public.outbound_emails IS 'Every system-sent email. gmail_message_id UNIQUE (multiple NULLs allowed). Commercial types queue for approval.';

CREATE TABLE public.inbound_email_records (
    gmail_message_id        text                        PRIMARY KEY,
    received_at             timestamptz                 NOT NULL,
    from_address            text                        NOT NULL,
    subject                 text,
    classified_as           text                        NOT NULL REFERENCES public.inbound_classification(code),
    classified_at           timestamptz                 NOT NULL DEFAULT now(),
    related_absence_id      bigint                      REFERENCES public.absences(id),
    related_order_id        bigint                      REFERENCES public.orders(id),
    related_enrolment_id    bigint                      REFERENCES public.enrolments(id)
);
COMMENT ON TABLE public.inbound_email_records IS 'Dedup table keyed on gmail_message_id; prevents double-processing across closely-spaced runs.';

-- --- Caterer weekly order summary (NEW) --------------------------------------
-- MOQ + finance are evaluated per caterer per week in the Thursday batch, not per
-- session. This is their home (moved off per-session orders).
CREATE TABLE public.caterer_week_orders (
    id                 bigserial   PRIMARY KEY,
    caterer_id         bigint      NOT NULL REFERENCES public.caterers(id),
    week_of            date        NOT NULL,        -- the week the Thursday batch covers
    run_id             bigint      REFERENCES public.agent_runs(id),
    total_items        integer     NOT NULL DEFAULT 0,
    variety_count      integer     NOT NULL DEFAULT 0,
    moq_min_total      integer,                     -- tier floor that applied
    moq_floor_applied  boolean     NOT NULL DEFAULT false,
    moq_variance_cents integer     NOT NULL DEFAULT 0,
    total_cost_cents   integer     NOT NULL DEFAULT 0,
    gst_rate_percent   numeric(5,2),
    summary_email_id   bigint      REFERENCES public.outbound_emails(id),
    composed_at        timestamptz NOT NULL DEFAULT now(),
    UNIQUE (caterer_id, week_of)
);
COMMENT ON TABLE public.caterer_week_orders IS 'Per-caterer, per-week batch summary: total meals, variety count, MOQ tier/floor/variance, consolidated cost, and the summary email. The grain at which MOQ and finance are evaluated in the Thursday batch.';

-- --- Case-book, escalations, annotations (NEW) -------------------------------
CREATE TABLE public.cases (
    id                  bigserial       PRIMARY KEY,
    situation           text            NOT NULL,
    decision            text,
    rationale           text,
    tags                text[],
    related_caterer_id  bigint          REFERENCES public.caterers(id),
    related_enrolment_id bigint         REFERENCES public.enrolments(id),
    related_run_id      bigint          REFERENCES public.agent_runs(id),
    created_at          timestamptz     NOT NULL DEFAULT now(),
    created_by          text
);
COMMENT ON TABLE public.cases IS 'Case-book of prior situation/decision/rationale, retrieved by keyword/recency. NOT truncated on demo resets.';

CREATE TABLE public.escalations (
    id                  bigserial       PRIMARY KEY,
    run_id              bigint          REFERENCES public.agent_runs(id),
    question            text            NOT NULL,
    context             jsonb,
    status              text            NOT NULL DEFAULT 'open' REFERENCES public.escalation_status(code),
    resolution          text,
    resolved_by         text,
    created_at          timestamptz     NOT NULL DEFAULT now(),
    resolved_at         timestamptz,
    related_caterer_id  bigint          REFERENCES public.caterers(id),
    related_enrolment_id bigint         REFERENCES public.enrolments(id),
    related_order_id    bigint          REFERENCES public.orders(id),
    related_step_id     bigint          REFERENCES public.agent_steps(id)
);
COMMENT ON TABLE public.escalations IS 'First-class escalation object. Resolutions feed student_eligible_meals (if dietary) and the case-book.';

CREATE TABLE public.decision_annotations (
    id            bigserial       PRIMARY KEY,
    step_id       bigint          REFERENCES public.agent_steps(id),
    run_id        bigint          REFERENCES public.agent_runs(id),
    comment       text            NOT NULL,
    author        text,
    created_at    timestamptz     NOT NULL DEFAULT now(),
    -- Feedback-sweep handling state (src/agent/feedback.py). handled_at NULL =
    -- un-actioned; claimed exactly once on handled_at so nothing is dropped/doubled.
    intent        text,
    handled_at    timestamptz,
    outcome       text,
    redo_run_id   bigint          REFERENCES public.agent_runs(id),
    redo_attempts integer         NOT NULL DEFAULT 0
);
COMMENT ON TABLE public.decision_annotations IS 'Operator comments on any agent step/decision (the "comment on any of them" UI). The feedback sweep classifies intent and either re-runs the task (instruction/rejection), stores a lesson (lesson/both), or escalates (unclear); handled_at marks it processed exactly once.';

-- Operator-authored authoritative policies (the editable BUSINESS-rule layer ON
-- TOP of the handbook's hard invariants). Every ACTIVE policy is injected into
-- the agent's always-on context as "[Policy #<id>]" and treated as binding.
-- Starts empty. See migration 007.
CREATE TABLE public.policies (
    id          bigserial    PRIMARY KEY,
    text        text         NOT NULL,
    active      boolean      NOT NULL DEFAULT true,
    sort_order  integer      NOT NULL DEFAULT 0,
    created_at  timestamptz  NOT NULL DEFAULT now(),
    updated_at  timestamptz  NOT NULL DEFAULT now()
);
COMMENT ON TABLE public.policies IS 'Operator-authored authoritative business rules, injected into the agent''s always-on context as "[Policy #<id>]" (active ones only). The editable layer ON TOP of the handbook''s hard invariants. Disable via active=false (reversible); sort_order then id orders how they read in context.';

-- Policy-citation traceability (parallel to step_lesson_citations): the active
-- policies a run cited as applied, parsed from "(applying Policy #<id>: <why>)".
-- See migration 008.
CREATE TABLE public.step_policy_citations (
    id          bigserial    PRIMARY KEY,
    run_id      bigint       NOT NULL REFERENCES public.agent_runs(id),
    step_id     bigint       REFERENCES public.agent_steps(id),
    policy_id   bigint       NOT NULL REFERENCES public.policies(id),
    reason      text,
    created_at  timestamptz  NOT NULL DEFAULT now()
);
COMMENT ON TABLE public.step_policy_citations IS 'Policy-citation traceability: the active policies a run actually CITED as applied (parsed from "(applying Policy #<id>: <why>)" in the reasoning), with the why. step_id = the cited decision/step; NULL = the run''s final answer. Only cited-as-applied, not every policy in context.';


-- =============================================================================
-- 3. INDEXES (secondary / performance — PK & UNIQUE are inline above)
-- =============================================================================

CREATE INDEX idx_enrolment_dietary_tags_enrolment ON public.enrolment_dietary_tags (enrolment_id);
CREATE INDEX idx_menu_item_dietary_tags_tag        ON public.menu_item_dietary_tags (dietary_tag_id);

CREATE INDEX idx_exclusions_school ON public.exclusions (school_id, start_date, end_date);
CREATE INDEX idx_exclusions_enrol  ON public.exclusions (enrolment_id, start_date, end_date);

CREATE INDEX idx_sta_slot_date ON public.session_tutor_assignments (session_slot_id, session_date);

CREATE INDEX idx_ess_session_slot ON public.enrolment_session_slots (session_slot_id);

CREATE INDEX idx_term_pref_enrol_caterer ON public.term_meal_preferences (enrolment_id, caterer_id);

CREATE INDEX idx_orders_caterer_date ON public.orders (caterer_id, session_date);

CREATE INDEX idx_order_lines_enrolment ON public.order_lines (enrolment_id);

CREATE INDEX idx_sem_menu_item ON public.student_eligible_meals (menu_item_id);

CREATE INDEX idx_feedback_caterer_submitted ON public.feedback (caterer_id, submitted_at);
CREATE INDEX idx_feedback_submitted         ON public.feedback (submitted_at);
CREATE INDEX idx_feedback_order             ON public.feedback (order_id);
CREATE INDEX idx_feedback_order_line        ON public.feedback (order_line_id);

CREATE INDEX idx_obi_enrol_emailsent ON public.opt_back_in_requests (enrolment_id, email_sent_at);

CREATE INDEX idx_agent_steps_urgency ON public.agent_steps (run_id, urgency, step_index);

CREATE INDEX idx_outbound_status        ON public.outbound_emails (status);
CREATE INDEX idx_outbound_type_address  ON public.outbound_emails (email_type, intended_to_address);

CREATE INDEX idx_cwo_run ON public.caterer_week_orders (run_id);

CREATE INDEX idx_cases_tags        ON public.cases USING gin (tags);
CREATE INDEX idx_cases_caterer     ON public.cases (related_caterer_id);
CREATE INDEX idx_cases_enrolment   ON public.cases (related_enrolment_id);

CREATE INDEX idx_escalations_status ON public.escalations (status);
CREATE INDEX idx_escalations_run    ON public.escalations (run_id);

CREATE INDEX idx_decision_annotations_step ON public.decision_annotations (step_id);
CREATE INDEX idx_decision_annotations_run  ON public.decision_annotations (run_id);

CREATE INDEX idx_policies_active_sort ON public.policies (active, sort_order, id);

CREATE UNIQUE INDEX idx_spc_run_policy ON public.step_policy_citations (run_id, policy_id);
CREATE INDEX idx_spc_step   ON public.step_policy_citations (step_id);
CREATE INDEX idx_spc_policy ON public.step_policy_citations (policy_id);


-- =============================================================================
-- 4. updated_at TRIGGERS (OPT-01) — same six core tables that carried it before
-- =============================================================================

DROP TRIGGER IF EXISTS trg_schools_updated_at        ON public.schools;
CREATE TRIGGER trg_schools_updated_at        BEFORE UPDATE ON public.schools        FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

DROP TRIGGER IF EXISTS trg_caterers_updated_at       ON public.caterers;
CREATE TRIGGER trg_caterers_updated_at       BEFORE UPDATE ON public.caterers       FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

DROP TRIGGER IF EXISTS trg_menu_items_updated_at     ON public.menu_items;
CREATE TRIGGER trg_menu_items_updated_at     BEFORE UPDATE ON public.menu_items     FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

DROP TRIGGER IF EXISTS trg_tutors_updated_at         ON public.tutors;
CREATE TRIGGER trg_tutors_updated_at         BEFORE UPDATE ON public.tutors         FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

DROP TRIGGER IF EXISTS trg_enrolments_updated_at     ON public.enrolments;
CREATE TRIGGER trg_enrolments_updated_at     BEFORE UPDATE ON public.enrolments     FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

DROP TRIGGER IF EXISTS trg_session_slots_updated_at  ON public.session_slots;
CREATE TRIGGER trg_session_slots_updated_at  BEFORE UPDATE ON public.session_slots  FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

-- Operator-editable policies also timestamp their edits (added with the table).
DROP TRIGGER IF EXISTS trg_policies_updated_at      ON public.policies;
CREATE TRIGGER trg_policies_updated_at       BEFORE UPDATE ON public.policies       FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

-- =============================================================================
-- END
-- =============================================================================
