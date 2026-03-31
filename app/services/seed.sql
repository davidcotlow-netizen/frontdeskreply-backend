-- ============================================================
-- Frontdesk AI — Seed SQL
-- Matches real table names from tasks.py and schemas.py
-- Run in Supabase SQL editor
-- ============================================================

-- 1. Business
INSERT INTO businesses (
  id,
  name,
  business_type,
  city,
  phone,
  hours,
  emergency_policy,
  service_areas,
  tone,
  email
) VALUES (
  '00000000-0000-0000-0000-000000000001',
  'Acme HVAC & Plumbing',
  'hvac',
  'Houston, TX',
  '7135550100',
  'Mon–Fri 8am–6pm, Sat 9am–2pm',
  'After-hours emergency calls accepted. Standard rate applies.',
  'Houston, TX — zip codes 77001–77099, 77401–77499',
  'professional but warm'
)
ON CONFLICT (id) DO NOTHING;

-- 2. Subscription plan (tasks.py fetches this — must exist or plan_tier defaults to "starter")
INSERT INTO subscription_plans (
  id,
  business_id,
  plan_tier,
  status
) VALUES (
  '00000000-0000-0000-0000-000000000002',
  '00000000-0000-0000-0000-000000000001',
  'starter',
  'active'
)
ON CONFLICT (id) DO NOTHING;

-- 3. Channel
INSERT INTO channels (
  id,
  business_id,
  channel_type,
  name
) VALUES (
  '00000000-0000-0000-0000-000000000001',
  '00000000-0000-0000-0000-000000000001',
  'web_form',
  'Website Contact Form'
)
ON CONFLICT (id) DO NOTHING;

-- 4. Contact
INSERT INTO contacts (
  id,
  business_id,
  name,
  phone
) VALUES (
  '00000000-0000-0000-0000-000000000010',
  '00000000-0000-0000-0000-000000000001',
  'Mike Johnson',
  '7135550182'
)
ON CONFLICT (id) DO NOTHING;

-- 5. Conversation
INSERT INTO conversations (
  id,
  business_id,
  contact_id,
  channel_type,
  status
) VALUES (
  '00000000-0000-0000-0000-000000000020',
  '00000000-0000-0000-0000-000000000001',
  '00000000-0000-0000-0000-000000000010',
  'web_form',
  'open'
)
ON CONFLICT (id) DO NOTHING;

-- ============================================================
-- Verification queries — each should return count = 1
-- ============================================================
SELECT 'businesses'          AS tbl, count(*) FROM businesses         WHERE id = '00000000-0000-0000-0000-000000000001'
UNION ALL
SELECT 'subscription_plans'  AS tbl, count(*) FROM subscription_plans WHERE business_id = '00000000-0000-0000-0000-000000000001' AND status = 'active'
UNION ALL
SELECT 'channels'            AS tbl, count(*) FROM channels            WHERE id = '00000000-0000-0000-0000-000000000001'
UNION ALL
SELECT 'contacts'            AS tbl, count(*) FROM contacts            WHERE id = '00000000-0000-0000-0000-000000000010'
UNION ALL
SELECT 'conversations'       AS tbl, count(*) FROM conversations       WHERE id = '00000000-0000-0000-0000-000000000020';
