#!/usr/bin/env bash
# Point d'entrée UNIQUE de la clé SSH du suivi automatique (Claude Code, 4×/jour).
# Installé comme « command= » forcée dans authorized_keys : cette clé ne peut RIEN faire d'autre.
#   ssh … report  → rapport complet en lecture seule (JSON)
#   ssh … notify  → envoie le texte reçu sur l'entrée standard sur Telegram (8 messages/jour max)
set -euo pipefail
cd /opt/apex
MODE="${SSH_ORIGINAL_COMMAND:-report}"
ARG="${MODE#* }"; [ "$ARG" = "$MODE" ] && ARG=""
MODE="${MODE%% *}"
echo "$(date -u '+%F %T') $MODE" >> /var/log/apex-followup.log   # lu par followup_watchdog.sh

case "$MODE" in
deploy)
  # déploiement d'une branche/commit du dépôt GitHub, avec tous les contrôles (voir safe_deploy.sh)
  exec /opt/apex/deploy/safe_deploy.sh "$ARG"
  ;;
audit)
  # vérification des données réelles contre la blockchain et DexScreener (lecture seule)
  docker compose exec -T notifier python -m apex.reporting.data_audit </dev/null
  ;;
logs)
  case "$ARG" in ingestor|features|labeler|learner|supervisor|notifier|dashboard|trader) ;;
    *) echo "service inconnu"; exit 2 ;; esac
  docker compose logs --no-color --since 6h --tail 200 "$ARG" 2>&1
  ;;
report)
  docker compose exec -T supervisor python - <<'PYEOF'
import asyncio, json, time
from apex.config import secrets
from apex.db import DB
from apex.bus import Bus

async def main():
    db, bus = await DB.connect(secrets().database_url, max_size=2), Bus(secrets().redis_url)
    q = lambda sql, *a: db.fetch(sql, *a)
    out = {"generated_at_utc": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())}
    st = await bus.get_json("apex:learner:state", {}) or {}
    out["learner"] = {
        "n_labels": st.get("n_labels"), "n_outcomes": st.get("n_outcomes"), "alerts_today": st.get("alerts_today"),
        "seconds_since_last_decision": round(time.time() - (st.get("last_decision_ts") or 0)),
        "seconds_since_last_label": round(time.time() - (st.get("last_label_ts") or 0)),
        "decision_latency_ms_p95": st.get("decision_latency_ms_p95"),
        "champions": {h: e["champion"] for h, e in (st.get("ensembles") or {}).items()},
        "logloss_vs_prior": {h: {c["id"]: c["logloss"] for c in e["competitors"] if c["id"] in (e["champion"], "prior")}
                             for h, e in (st.get("ensembles") or {}).items()},
        "bandit_active": (st.get("bandit") or {}).get("active_detail"),
        "bandit_best_by_policy": (st.get("bandit") or {}).get("best_by_policy"),
    }
    for k in ("apex:learning:health", "apex:trading:status", "apex:claude:status", "apex:ingestor:stats",
              "apex:labeler:stats", "apex:enrichment", "apex:metadata", "apex:market", "apex:trader:stats"):
        out[k.split(":", 1)[1]] = await bus.get_json(k, {})
    ing = out.get("ingestor:stats") or {}
    ing.pop("bytes", None)
    hb = await bus.heartbeats()
    out["heartbeat_age_s"] = {s: round(time.time() - t) for s, t in hb.items()}
    sup = await bus.get_json("apex:supervisor:last_cycle", {}) or {}
    out["supervisor"] = {"age_s": round(time.time() - sup.get("ts", 0)), "duration_s": sup.get("duration_s"),
                         "events": sup.get("events", [])[:8],
                         "abnormal_states": [f"{s['curve']}:{s['state']}" for s in sup.get("states", [])
                                             if s["state"] not in ("STABLE", "DONNEES_INSUFFISANTES", "PROGRESSION")]}
    rows = lambda rs: [dict(r) for r in rs]
    out["paper"] = rows(await q("""SELECT status, count(*) n, round(sum(pnl_sol)::numeric, 4) pnl_sol,
                                   round(avg(pnl)::numeric, 4) avg_pnl FROM paper_positions GROUP BY 1"""))
    out["paper_last_24h"] = rows(await q("""SELECT policy, count(*) n, round(avg(pnl)::numeric, 4) avg_pnl,
                                            count(*) FILTER (WHERE pnl > 0) wins FROM paper_positions
                                            WHERE status='closed' AND closed_at > now() - interval '24 hours' GROUP BY 1"""))
    out["execution"] = rows(await q("""SELECT mode, status, count(*) n, round(sum(pnl_sol)::numeric, 4) pnl_sol,
                                       round(avg(pnl)::numeric, 4) avg_pnl FROM exec_positions GROUP BY 1, 2"""))
    out["orders_24h"] = rows(await q("""SELECT side, status, count(*) n, round(avg(slippage)::numeric, 4) avg_slippage,
                                        count(*) FILTER (WHERE builder_ok) pumpportal_ok FROM exec_orders
                                        WHERE ts > now() - interval '24 hours' GROUP BY 1, 2"""))
    out["alerts_24h"] = (await q("SELECT count(*) n FROM alerts WHERE ts > now() - interval '24 hours'"))[0]["n"]
    out["outcomes_by_policy_24h"] = rows(await q("""SELECT key policy, count(*) n, round(avg(value::float)::numeric, 4) avg_pnl
        FROM outcomes, jsonb_each_text(pnl) WHERE ts > now() - interval '24 hours' GROUP BY 1 ORDER BY 3 DESC"""))
    out["labels_last_hour"] = rows(await q("""SELECT horizon, count(*) n, round(avg(y)::numeric, 4) pos_rate FROM labels
                                              WHERE ts > now() - interval '1 hour' GROUP BY 1 ORDER BY 1"""))
    out["errors_6h"] = rows(await q("""SELECT error_type, count(*) n, round(sum(cost)::numeric, 2) cost FROM errors
                                       WHERE ts > now() - interval '6 hours' GROUP BY 1 ORDER BY 2 DESC"""))
    out["corrections_24h"] = rows(await q("""SELECT id, action, problem_key, status, effect->>'verdict' verdict FROM corrections
                                             WHERE ts > now() - interval '24 hours' ORDER BY id DESC LIMIT 15"""))
    out["claude_features"] = rows(await q("SELECT status, count(*) n FROM claude_features GROUP BY 1"))
    out["claude_last_proposal"] = str((await q("SELECT max(ts) t FROM claude_proposals"))[0]["t"])
    out["system_events_6h"] = rows(await q("""SELECT ts::text, kind, left(message, 160) msg FROM system_events
        WHERE ts > now() - interval '6 hours' AND kind IN ('urgent','rollback','trading','lgbm','champion') ORDER BY ts DESC LIMIT 20"""))
    print(json.dumps(out, default=str, ensure_ascii=False))

asyncio.run(main())
PYEOF
  echo "### tracebacks (6 h) par service"
  for s in ingestor features labeler learner supervisor notifier dashboard; do
    echo "$s: $(docker compose logs --no-color --since 6h "$s" 2>&1 | grep -c Traceback || true)"
  done
  echo "### dernières erreurs"
  docker compose logs --no-color --since 6h 2>&1 | grep -E "Error|ERROR" | grep -v Timescale | tail -8 || true
  echo "### version en production"
  git -C /opt/apex log -1 --format="%h %ci %s" 2>/dev/null || true
  echo "### derniers déploiements"
  tail -6 /var/log/apex-deploys.log 2>/dev/null || echo "aucun"
  echo "### ressources"
  free -h | head -2; df -h / | tail -1
  docker stats --no-stream --format "{{.Name}} {{.CPUPerc}} {{.MemUsage}}"
  ;;
notify)
  TEXT="$(head -c 3500)"   # à lire AVANT tout appel docker (sinon « docker compose exec » avale l'entrée)
  [ -n "$TEXT" ] || { echo "message vide"; exit 2; }
  KEY="apex:followup:sent:$(date -u +%Y%m%d)"
  N=$(docker compose exec -T redis redis-cli incr "$KEY" </dev/null | tr -d '\r')
  docker compose exec -T redis redis-cli expire "$KEY" 172800 </dev/null >/dev/null
  if [ "${N:-99}" -gt 8 ]; then echo "limite quotidienne de messages atteinte"; exit 0; fi
  envval() { grep -E "^$1=" /opt/apex/.env | head -1 | cut -d= -f2- | tr -d '\r'; }
  TELEGRAM_BOT_TOKEN="$(envval TELEGRAM_BOT_TOKEN)"; TELEGRAM_CHAT_ID="$(envval TELEGRAM_CHAT_ID)"
  curl -s -o /dev/null -w "%{http_code}\n" "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
       --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" --data-urlencode "disable_web_page_preview=true" \
       --data-urlencode "text=🤖 Suivi Claude Code
${TEXT}"
  ;;
*)
  echo "commande non autorisée (report | audit | notify | logs <service> | deploy <branche>)"; exit 1 ;;
esac
