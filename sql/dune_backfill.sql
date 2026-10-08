-- Requête Dune (DuneSQL) pour le backfill pump.fun.
-- À créer sur dune.com (New query), puis renseigner son id dans config.yaml (backfill.dune_query_id).
-- Paramètres : start (timestamp), end (timestamp), sample_mod (nombre : 1 token sur N).
-- ⚠ Vérifie le nom des tables/colonnes décodées dans l'explorateur Dune (pumpdotfun_solana.*) :
--   pump.fun fait évoluer son IDL ; adapter les noms si besoin.
WITH created AS (
    SELECT evt_block_time AS t0, evt_block_slot AS slot0, evt_tx_id AS sig, mint, name, symbol, uri,
           bondingCurve AS bonding_curve, "user" AS creator
    FROM pumpdotfun_solana.pump_evt_createevent
    WHERE evt_block_time >= TIMESTAMP '{{start}}' AND evt_block_time < TIMESTAMP '{{end}}'
      AND mod(from_big_endian_64(substr(sha256(to_utf8(mint)), 1, 8)), {{sample_mod}}) = 0
)
SELECT 'create' AS kind, to_unixtime(c.t0) AS ts, c.slot0 AS slot, c.sig AS signature, c.mint,
       c.name, c.symbol, c.uri, c.bonding_curve, c.creator,
       NULL AS trader, NULL AS is_buy, NULL AS sol, NULL AS tokens, NULL AS v_sol, NULL AS v_tokens
FROM created c
UNION ALL
SELECT 'trade', to_unixtime(t.evt_block_time), t.evt_block_slot, t.evt_tx_id, t.mint,
       NULL, NULL, NULL, NULL, NULL,
       t."user", t.isBuy, t.solAmount / 1e9, t.tokenAmount / 1e6,
       t.virtualSolReserves / 1e9, t.virtualTokenReserves / 1e6
FROM pumpdotfun_solana.pump_evt_tradeevent t
JOIN created c ON c.mint = t.mint
WHERE t.evt_block_time >= c.t0 AND t.evt_block_time < c.t0 + INTERVAL '75' MINUTE
UNION ALL
SELECT 'migration', to_unixtime(e.evt_block_time), e.evt_block_slot, e.evt_tx_id, e.mint,
       NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL
FROM pumpdotfun_solana.pump_evt_completeevent e
JOIN created c ON c.mint = e.mint
WHERE e.evt_block_time < c.t0 + INTERVAL '75' MINUTE
ORDER BY 2, 3
