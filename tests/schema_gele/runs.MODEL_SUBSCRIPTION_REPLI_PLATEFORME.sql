ALTER TABLE org_model_subscription_modes ADD COLUMN IF NOT EXISTS repli_cle_plateforme BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE org_model_subscription_modes ADD COLUMN IF NOT EXISTS repli_cle_plateforme_by TEXT;
ALTER TABLE org_model_subscription_modes ADD COLUMN IF NOT EXISTS repli_cle_plateforme_at TIMESTAMPTZ;
