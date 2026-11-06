CREATE TABLE IF NOT EXISTS users (
 id text PRIMARY KEY, email text UNIQUE NOT NULL, name text NOT NULL, password_hash text NOT NULL,
 role text NOT NULL DEFAULT 'member' CHECK(role IN ('member','admin')), status text NOT NULL DEFAULT 'active' CHECK(status IN ('active','disabled')),
 balance bigint NOT NULL DEFAULT 0 CHECK(balance>=0), frozen bigint NOT NULL DEFAULT 0 CHECK(frozen>=0),
 fee_override bigint CHECK(fee_override>=0), invite_code text UNIQUE NOT NULL, invited_by text REFERENCES users(id),
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS sessions (hash text PRIMARY KEY,user_id text REFERENCES users(id) ON DELETE CASCADE,expires_at timestamptz NOT NULL);
ALTER TABLE users ADD COLUMN IF NOT EXISTS deleted_at timestamptz;
ALTER TABLE users DROP CONSTRAINT IF EXISTS users_email_key;
CREATE UNIQUE INDEX IF NOT EXISTS users_live_email_unique ON users(email) WHERE deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS api_keys (id text PRIMARY KEY,user_id text NOT NULL REFERENCES users(id),name text NOT NULL,hash text UNIQUE NOT NULL,prefix text NOT NULL,revoked boolean NOT NULL DEFAULT false,last_used timestamptz,created_at timestamptz DEFAULT now());

CREATE TABLE IF NOT EXISTS settings (key text PRIMARY KEY,value jsonb NOT NULL);
CREATE TABLE IF NOT EXISTS email_challenges (
 email text NOT NULL,purpose text NOT NULL CHECK(purpose IN ('register','reset')),
 secret_hash text NOT NULL,user_id text REFERENCES users(id) ON DELETE CASCADE,
 credential_hash text NOT NULL DEFAULT '',attempts int NOT NULL DEFAULT 0,
 delivered boolean NOT NULL DEFAULT false,created_at timestamptz NOT NULL DEFAULT now(),
 expires_at timestamptz NOT NULL,PRIMARY KEY(email,purpose)
);
CREATE INDEX IF NOT EXISTS email_challenges_expiry ON email_challenges(expires_at);
CREATE TABLE IF NOT EXISTS mail_deliveries (
 id text PRIMARY KEY,recipient text NOT NULL,purpose text NOT NULL CHECK(purpose IN ('register','reset','test')),
 status text NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','sent','failed')),
 detail text NOT NULL DEFAULT '',created_at timestamptz NOT NULL DEFAULT now(),completed_at timestamptz
);
CREATE INDEX IF NOT EXISTS mail_deliveries_created ON mail_deliveries(created_at DESC);

CREATE TABLE IF NOT EXISTS products (id text PRIMARY KEY,name text NOT NULL,description text NOT NULL,fee bigint NOT NULL CHECK(fee>=0),enabled boolean NOT NULL DEFAULT true,sort int NOT NULL DEFAULT 0,tag text NOT NULL DEFAULT '',features jsonb NOT NULL DEFAULT '[]');

CREATE TABLE IF NOT EXISTS orders (
 id text PRIMARY KEY,user_id text NOT NULL REFERENCES users(id),product_id text NOT NULL REFERENCES products(id),product_name text NOT NULL,
 email text NOT NULL,country text NOT NULL DEFAULT 'PH',seats int NOT NULL DEFAULT 1 CHECK(seats BETWEEN 1 AND 100),
 status text NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','queued','processing','awaiting_payment','succeeded','failed','cancelled','review')),
 fee bigint NOT NULL CHECK(fee>=0),source text NOT NULL DEFAULT 'web',note text NOT NULL DEFAULT '',renewal text NOT NULL DEFAULT 'not_started',
 idempotency_key text NOT NULL,request_hash text NOT NULL,created_at timestamptz NOT NULL DEFAULT now(),updated_at timestamptz NOT NULL DEFAULT now(),UNIQUE(user_id,idempotency_key)
);

CREATE INDEX IF NOT EXISTS orders_user_time ON orders(user_id,created_at DESC);

CREATE TABLE IF NOT EXISTS order_events (id bigserial PRIMARY KEY,order_id text NOT NULL REFERENCES orders(id),stage text NOT NULL,message text NOT NULL,created_at timestamptz DEFAULT now());

CREATE TABLE IF NOT EXISTS payment_trace_events (
 id bigserial PRIMARY KEY, order_id text NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
 phase text NOT NULL, state text NOT NULL, code text NOT NULL DEFAULT '',
 round int, elapsed_ms bigint NOT NULL DEFAULT 0, http_status int NOT NULL DEFAULT 0,
 request_id text NOT NULL DEFAULT '', diagnostics jsonb NOT NULL DEFAULT '{}',
 created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS payment_trace_order_idx ON payment_trace_events(order_id,id);

CREATE TABLE IF NOT EXISTS ledger (
 id bigserial PRIMARY KEY,user_id text NOT NULL REFERENCES users(id),kind text NOT NULL,amount bigint NOT NULL,balance_after bigint NOT NULL,frozen_after bigint NOT NULL,
 reference text NOT NULL,description text NOT NULL,actor_id text REFERENCES users(id),created_at timestamptz NOT NULL DEFAULT now(),UNIQUE(user_id,reference)
);

CREATE INDEX IF NOT EXISTS ledger_user_time ON ledger(user_id,created_at DESC);

CREATE TABLE IF NOT EXISTS topups (id text PRIMARY KEY,user_id text NOT NULL REFERENCES users(id),amount bigint NOT NULL CHECK(amount>0),method text NOT NULL DEFAULT 'manual',proof text NOT NULL DEFAULT '',status text NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected','cancelled')),note text NOT NULL DEFAULT '',reviewed_by text REFERENCES users(id),created_at timestamptz NOT NULL DEFAULT now(),updated_at timestamptz NOT NULL DEFAULT now());

CREATE TABLE IF NOT EXISTS redeem_codes (id text PRIMARY KEY,code_hash text UNIQUE NOT NULL,code_enc text NOT NULL,prefix text NOT NULL,amount bigint NOT NULL CHECK(amount>0),batch text NOT NULL,status text NOT NULL DEFAULT 'active' CHECK(status IN ('active','used','disabled')),used_by text REFERENCES users(id),used_at timestamptz,expires_at timestamptz NOT NULL,created_at timestamptz DEFAULT now());
CREATE INDEX IF NOT EXISTS redeem_codes_used_at ON redeem_codes(used_at DESC,id DESC) WHERE used_by IS NOT NULL;

CREATE TABLE IF NOT EXISTS announcements (id text PRIMARY KEY,title text NOT NULL,body text NOT NULL,tone text NOT NULL DEFAULT 'info',published boolean NOT NULL DEFAULT false,login_visible boolean NOT NULL DEFAULT false,sort int NOT NULL DEFAULT 0,starts_at timestamptz,ends_at timestamptz,created_at timestamptz DEFAULT now());

CREATE TABLE IF NOT EXISTS resources (id text PRIMARY KEY,name text NOT NULL,kind text NOT NULL DEFAULT 'proxy',country text NOT NULL DEFAULT 'PH',endpoint_enc text NOT NULL,enabled boolean NOT NULL DEFAULT true,weight int NOT NULL DEFAULT 1,capacity int NOT NULL DEFAULT 1,last_status text NOT NULL DEFAULT 'untested',last_checked timestamptz,created_at timestamptz DEFAULT now());

ALTER TABLE resources ADD COLUMN IF NOT EXISTS vendor text NOT NULL DEFAULT '' CHECK (vendor IN ('','iproyal','cliproxy','other'));

ALTER TABLE resources ADD COLUMN IF NOT EXISTS ip_mode text NOT NULL DEFAULT 'dynamic' CHECK (ip_mode IN ('dynamic','static'));

CREATE TABLE IF NOT EXISTS risk_events (id bigserial PRIMARY KEY,user_id text REFERENCES users(id),category text NOT NULL,severity text NOT NULL,ip text NOT NULL,detail text NOT NULL,resolved boolean NOT NULL DEFAULT false,created_at timestamptz DEFAULT now());

CREATE INDEX IF NOT EXISTS risk_time ON risk_events(created_at DESC);
ALTER TABLE risk_events ADD COLUMN IF NOT EXISTS occurrences int NOT NULL DEFAULT 1;
ALTER TABLE risk_events ADD COLUMN IF NOT EXISTS last_seen timestamptz;

CREATE TABLE IF NOT EXISTS audit_events (id bigserial PRIMARY KEY,actor_id text REFERENCES users(id),action text NOT NULL,target text NOT NULL,detail text NOT NULL,ip text NOT NULL,created_at timestamptz DEFAULT now());

CREATE TABLE IF NOT EXISTS rate_buckets (key text PRIMARY KEY,hits int NOT NULL,expires_at timestamptz NOT NULL);

CREATE TABLE IF NOT EXISTS blocked_ips (ip text PRIMARY KEY,reason text NOT NULL,expires_at timestamptz NOT NULL,created_at timestamptz DEFAULT now());

CREATE TABLE IF NOT EXISTS mutation_keys (key text PRIMARY KEY,actor_id text NOT NULL,request_hash text NOT NULL,result jsonb NOT NULL,created_at timestamptz DEFAULT now());

ALTER TABLE users ADD COLUMN IF NOT EXISTS membership_code text NOT NULL DEFAULT '';

ALTER TABLE users ADD COLUMN IF NOT EXISTS membership_until timestamptz;

CREATE TABLE IF NOT EXISTS user_product_prices (
 user_id text NOT NULL REFERENCES users(id),product_id text NOT NULL REFERENCES products(id),
 fee bigint NOT NULL CHECK(fee>=0),updated_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(user_id,product_id)
);

ALTER TABLE orders ADD COLUMN IF NOT EXISTS deleted_at timestamptz;

ALTER TABLE orders ADD COLUMN IF NOT EXISTS price_snapshot jsonb NOT NULL DEFAULT '{}';

ALTER TABLE ledger ADD COLUMN IF NOT EXISTS order_id text REFERENCES orders(id);

CREATE INDEX IF NOT EXISTS ledger_order_membership ON ledger(user_id,order_id) WHERE kind IN ('charge','refund');

ALTER TABLE resources ADD COLUMN IF NOT EXISTS last_ip text NOT NULL DEFAULT '';

ALTER TABLE resources ADD COLUMN IF NOT EXISTS last_country text NOT NULL DEFAULT '';

ALTER TABLE resources ADD COLUMN IF NOT EXISTS last_latency_ms bigint NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS proxy_leases (
 id text PRIMARY KEY, resource_id text NOT NULL REFERENCES resources(id),
 expires_at timestamptz NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS proxy_leases_capacity ON proxy_leases(resource_id,expires_at);

ALTER TABLE orders ADD COLUMN IF NOT EXISTS billing_address jsonb NOT NULL DEFAULT '{}';
ALTER TABLE orders ADD COLUMN IF NOT EXISTS region_snapshot jsonb NOT NULL DEFAULT '{}';

ALTER TABLE orders ADD COLUMN IF NOT EXISTS workflow text NOT NULL DEFAULT 'checkout';

ALTER TABLE orders ADD COLUMN IF NOT EXISTS keep_renewal boolean NOT NULL DEFAULT false;

CREATE TABLE IF NOT EXISTS renewal_tasks (
 id text PRIMARY KEY,user_id text NOT NULL REFERENCES users(id),email text NOT NULL,account_hash text NOT NULL,
 status text NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','processing','checking','disabled','no_subscription','review')),
 message text NOT NULL DEFAULT '申请已受理，等待处理。',
 credentials_enc text NOT NULL,credentials_expires_at timestamptz NOT NULL,
 cancel_started boolean NOT NULL DEFAULT false,checks int NOT NULL DEFAULT 0,
 next_at timestamptz NOT NULL DEFAULT now(),lease_token text,lease_until timestamptz,
 created_at timestamptz NOT NULL DEFAULT now(),updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS renewal_account_pending ON renewal_tasks(account_hash) WHERE status IN ('queued','processing','checking','review');

CREATE INDEX IF NOT EXISTS renewal_tasks_queue ON renewal_tasks(next_at) WHERE status IN ('queued','processing','checking');

CREATE TABLE IF NOT EXISTS renewal_request_keys (
 user_id text NOT NULL REFERENCES users(id),key text NOT NULL,account_hash text NOT NULL,
 task_id text NOT NULL REFERENCES renewal_tasks(id),PRIMARY KEY(user_id,key)
);

CREATE TABLE IF NOT EXISTS renewal_events (
 id bigserial PRIMARY KEY,task_id text NOT NULL REFERENCES renewal_tasks(id),
 status text NOT NULL,message text NOT NULL,created_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE resources ADD COLUMN IF NOT EXISTS quality_result jsonb NOT NULL DEFAULT '{}';

ALTER TABLE resources ADD COLUMN IF NOT EXISTS openai_result jsonb NOT NULL DEFAULT '{}';

CREATE TABLE IF NOT EXISTS proxy_checks (
 id text PRIMARY KEY,resource_id text NOT NULL REFERENCES resources(id) ON DELETE CASCADE,
 kind text NOT NULL CHECK(kind IN ('quality','openai')),state text NOT NULL DEFAULT 'queued' CHECK(state IN ('queued','running','completed')),
 result jsonb NOT NULL DEFAULT '{}',lease_token text,lease_until timestamptz,proxy_lease_id text,
 created_at timestamptz NOT NULL DEFAULT now(),updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS proxy_checks_pending ON proxy_checks(resource_id) WHERE state IN ('queued','running');

ALTER TABLE orders ADD COLUMN IF NOT EXISTS activation text NOT NULL DEFAULT 'not_checked';

CREATE TABLE IF NOT EXISTS order_closures (
 order_id text PRIMARY KEY REFERENCES orders(id),actor_id text NOT NULL REFERENCES users(id),
 reason text NOT NULL,prior_status text NOT NULL,prior_payment_state text NOT NULL,payment_submitted boolean NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS order_proxy_sessions (
 order_id text PRIMARY KEY REFERENCES orders(id),
 resource_id text NOT NULL,
 endpoint_enc text NOT NULL,
 expected_ip text NOT NULL DEFAULT '',
 expires_at timestamptz NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS subscription_runs (
 order_id text PRIMARY KEY REFERENCES orders(id),
 user_id text NOT NULL REFERENCES users(id),
 credentials_enc text NOT NULL DEFAULT '',
 credentials_expires_at timestamptz NOT NULL DEFAULT now(),
 card_enc text NOT NULL DEFAULT '',
 workspace_name text NOT NULL DEFAULT 'work',
 premium_seats int NOT NULL DEFAULT 1,
 billing_snapshot jsonb NOT NULL DEFAULT '{}',
 session_id text NOT NULL DEFAULT '',
 checkout_url_enc text NOT NULL DEFAULT '',
 checkout_session_id text NOT NULL DEFAULT '',
 payment_last4 text NOT NULL DEFAULT '',
 payment_amount bigint,
 payment_currency text NOT NULL DEFAULT '',
 paid_at timestamptz,
 created_at timestamptz NOT NULL DEFAULT now(),
 updated_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE subscription_runs ADD COLUMN IF NOT EXISTS trace_round int NOT NULL DEFAULT 0;
-- Written at admission; changing the site default never reassigns existing jobs.
-- Legacy rows with unknown ownership must not be guessed from the current setting.
ALTER TABLE subscription_runs ADD COLUMN IF NOT EXISTS subscription_flow text NOT NULL DEFAULT '';
-- Existing columns keep their original inline CHECK; refresh it explicitly so new flows are allowed.
ALTER TABLE subscription_runs DROP CONSTRAINT IF EXISTS subscription_runs_subscription_flow_check;
ALTER TABLE subscription_runs ADD CONSTRAINT subscription_runs_subscription_flow_check
 CHECK (subscription_flow IN ('', 'gener_direct_pay', 'cslive_direct_pay', 'cslive_go_direct_pay', 'cslive_full_go'));
ALTER TABLE subscription_runs ADD COLUMN IF NOT EXISTS checkout_checkpoint_enc text NOT NULL DEFAULT '';
ALTER TABLE subscription_runs ADD COLUMN IF NOT EXISTS network_retry_count int NOT NULL DEFAULT 0 CHECK(network_retry_count>=0);
ALTER TABLE subscription_runs ADD COLUMN IF NOT EXISTS network_retry_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE subscription_runs ADD COLUMN IF NOT EXISTS payment_submit_started_at timestamptz;

-- Only created after durable payment success. Contains no card or payment-submit state.
CREATE TABLE IF NOT EXISTS subscription_followups (
 order_id text PRIMARY KEY REFERENCES orders(id),
 checkpoint_enc text NOT NULL,
 phase text NOT NULL,
 warning_code text NOT NULL DEFAULT '',
 created_at timestamptz NOT NULL DEFAULT now(),
 updated_at timestamptz NOT NULL DEFAULT now()
);

-- Delivered only after commit; no polling and no customer/credential data.
CREATE OR REPLACE FUNCTION notify_order_update() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE oid text; owner_id text;
BEGIN
 IF TG_TABLE_NAME='orders' THEN
  oid=NEW.id; owner_id=NEW.user_id;
 ELSE
  oid=NEW.order_id;
  SELECT user_id INTO owner_id FROM orders WHERE id=oid;
 END IF;
 IF owner_id IS NOT NULL THEN
  PERFORM pg_notify('xufu_order_updates',json_build_object('id',oid,'user_id',owner_id)::text);
 END IF;
 RETURN NEW;
END $$;

-- Only new admissions participate. Reinitialization never backfills old orders.
CREATE TABLE IF NOT EXISTS invitation_candidates (
 order_id text PRIMARY KEY REFERENCES orders(id),invitee_id text NOT NULL REFERENCES users(id),
 inviter_id text NOT NULL REFERENCES users(id),rate_percent int NOT NULL CHECK(rate_percent BETWEEN 1 AND 30),
 created_at timestamptz NOT NULL DEFAULT now(),CHECK(inviter_id<>invitee_id)
);
CREATE TABLE IF NOT EXISTS invitation_rewards (
 invitee_id text PRIMARY KEY REFERENCES users(id),inviter_id text NOT NULL REFERENCES users(id),
 order_id text UNIQUE NOT NULL REFERENCES orders(id),rate_percent int NOT NULL CHECK(rate_percent BETWEEN 1 AND 30),
 fee bigint NOT NULL CHECK(fee>=0),granted bigint NOT NULL CHECK(granted>=0),
 entitled bigint NOT NULL CHECK(entitled>=0 AND entitled<=granted),recovered bigint NOT NULL DEFAULT 0 CHECK(recovered>=0 AND recovered<=granted),
 created_at timestamptz NOT NULL DEFAULT now(),updated_at timestamptz NOT NULL DEFAULT now(),CHECK(inviter_id<>invitee_id)
);
CREATE INDEX IF NOT EXISTS invitation_reward_owner ON invitation_rewards(inviter_id);
CREATE OR REPLACE FUNCTION snapshot_invitation() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE inviter text; rules jsonb;
BEGIN
 IF NEW.workflow<>'subscription' THEN RETURN NEW; END IF;
 SELECT value INTO rules FROM settings WHERE key='invites';
 IF rules->>'enabled' IS DISTINCT FROM 'true' THEN RETURN NEW; END IF;
 SELECT invited_by INTO inviter FROM users WHERE id=NEW.user_id;
 IF inviter IS NOT NULL AND inviter<>NEW.user_id THEN
  INSERT INTO invitation_candidates(order_id,invitee_id,inviter_id,rate_percent)
  VALUES(NEW.id,NEW.user_id,inviter,(rules->>'rate_percent')::int) ON CONFLICT DO NOTHING;
 END IF;
 RETURN NEW;
END $$;
DO $$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM pg_trigger WHERE tgname='invitation_admission' AND tgrelid='orders'::regclass) THEN
  CREATE TRIGGER invitation_admission AFTER INSERT ON orders FOR EACH ROW EXECUTE FUNCTION snapshot_invitation();
 END IF;
END $$;
DO $$
DECLARE table_name text;
BEGIN
 FOREACH table_name IN ARRAY ARRAY['orders','order_events','payment_trace_events','subscription_runs','subscription_followups'] LOOP
  IF NOT EXISTS(SELECT 1 FROM pg_trigger WHERE tgname='order_update_notify' AND tgrelid=table_name::regclass) THEN
   EXECUTE format('CREATE TRIGGER order_update_notify AFTER INSERT OR UPDATE ON %I FOR EACH ROW EXECUTE FUNCTION notify_order_update()',table_name);
  END IF;
 END LOOP;
END $$;

-- Country-pool bindings preserve original endpoints and expiration across retries.
ALTER TABLE order_proxy_sessions ADD COLUMN IF NOT EXISTS source_mode text NOT NULL DEFAULT 'manual';
ALTER TABLE order_proxy_sessions ADD COLUMN IF NOT EXISTS session_ref text NOT NULL DEFAULT '';
CREATE TABLE IF NOT EXISTS country_pool_sessions (
 session_id text PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS renewal_proxy_sessions (
 task_id text PRIMARY KEY REFERENCES renewal_tasks(id), endpoint_enc text NOT NULL,
 expires_at timestamptz NOT NULL, session_ref text NOT NULL, country text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now()
);
