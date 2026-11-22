ALTER TABLE org_model_subscription_modes ADD COLUMN IF NOT EXISTS repli_quand TEXT NOT NULL DEFAULT 'plafond' CHECK (repli_quand IN ('plafond', 'plafond_ou_deconnexion'));
ALTER TABLE org_model_subscription_modes ADD COLUMN IF NOT EXISTS repli_quand_by TEXT;
ALTER TABLE org_model_subscription_modes ADD COLUMN IF NOT EXISTS repli_quand_at TIMESTAMPTZ;
