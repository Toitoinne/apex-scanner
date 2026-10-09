-- Smart wallets v2 : gains réellement encaissés, robots et snipers écartés (voir apex/features/wallets.py)
ALTER TABLE wallets ADD COLUMN IF NOT EXISTS fast_flips INT DEFAULT 0;
ALTER TABLE wallets ADD COLUMN IF NOT EXISTS snipes INT DEFAULT 0;
ALTER TABLE wallets ADD COLUMN IF NOT EXISTS first_trade TIMESTAMPTZ;
