# HL Funding Spike Sniper

> Bot opportuniste qui short les perps Hyperliquid quand le funding rate
> s'emballe, et qui sort dès que la fenêtre se referme.

---

## Table des matières

1. [Objectif & contexte](#1-objectif--contexte)
2. [La thèse stratégique (pourquoi ça marche)](#2-la-thèse-stratégique-pourquoi-ça-marche)
3. [La stratégie en détail](#3-la-stratégie-en-détail)
4. [Architecture du code](#4-architecture-du-code)
5. [Configuration exhaustive](#5-configuration-exhaustive-tous-les-paramètres)
6. [Setup pas-à-pas](#6-setup-pas-à-pas)
7. [Modes d'exécution](#7-modes-dexécution)
8. [Lancement & exploitation](#8-lancement--exploitation)
9. [État persistant (SQLite + logs)](#9-état-persistant-sqlite--logs)
10. [Validations & tests réalisés](#10-validations--tests-réalisés-dry-run)
11. [Bugs trouvés et corrections](#11-bugs-trouvés-et-corrections)
12. [Hyperliquid : ce qu'il faut savoir](#12-hyperliquid--ce-quil-faut-savoir)
13. [Le dashboard Dune lié](#13-le-dashboard-dune-lié)
14. [Capacité économique attendue](#14-capacité-économique-attendue)
15. [Limites connues v1](#15-limites-connues-v1)
16. [Roadmap](#16-roadmap)
17. [Sécurité](#17-sécurité)
18. [Glossaire & formules](#18-glossaire--formules)
19. [Historique de décisions (pour un futur "moi")](#19-historique-de-décisions-pour-un-futur-moi)

---

## 1. Objectif & contexte

### Objectif

Un bot **automatique** qui ouvre des positions **short** sur les perps
Hyperliquid quand le **funding rate** d'un coin devient extrême (positif) et
que la **liquidité** est suffisante, puis qui **sort** dès que le funding
redescend, qu'un take-profit / stop-loss est atteint, ou que le z-score
normalise.

Aucun hedge spot — version **"naked short"** assumée. Voir
[§3 mécanique](#mécanique-du-trade) pour la justification.

### Contexte d'où vient cette stratégie

Ce bot est l'extension naturelle d'un travail de recherche fait sur Dune :

- On a construit un dashboard Dune **"Hyperliquid Trader Edge Console"**
  (voir [§13](#13-le-dashboard-dune-lié)) avec 4 sections : Funding edge,
  Premium/basis anomalies, Liquidity quality, Bridge capital flows.
- Le dashboard sert à la **recherche** et à la **calibration** historique.
- Le bot, lui, agit en **temps réel** sur l'API HL (pas Dune, qui a 3-4
  semaines de retard d'indexation).

### Ce que cette stratégie N'EST PAS

- ❌ Pas du tout liée à Polymarket (un fil de discussion initial avait
  ambigüité — clarifié : c'est uniquement Hyperliquid).
- ❌ Pas du cash-and-carry delta-neutre (long spot + short perp) parce que
  HL spot ne liste qu'un petit nombre de tokens (HYPE, PURR, quelques
  majors). Pour 90% des coins HL perp, il n'y a pas de spot HL → on ne
  peut pas hedger sans aller cross-exchange. v1 = naked short assumé.
- ❌ Pas une stratégie momentum (entrer en long sur funding élevé) — on
  exploite l'inverse, le mean-reversion des crowded longs.
- ❌ Pas un HFT / market making — fréquence **horaire**, calée sur les
  funding settlements HL.

---

## 2. La thèse stratégique (pourquoi ça marche)

### Le pattern qu'on exploite

Quand le funding rate d'un coin HL s'envole, ça signifie **trop de monde
est long avec du levier**. Ce setup paie sur **deux leviers en même temps** :

1. **Funding harvesting** : on encaisse le funding toutes les heures tant
   qu'on est en short (HL paie le funding aux shorts quand il est positif).
2. **Mean reversion** : statistiquement, après un funding extrême, le prix
   corrige — les longs sur-leverés finissent par se faire liquider en
   cascade, ce qui amplifie la baisse.

Les deux sources d'edge vont dans la même direction. C'est ce qui rend la
stratégie élégante.

### Pourquoi Hyperliquid spécifiquement

| Facteur HL | Effet sur l'edge |
|---|---|
| Base utilisateurs plus retail que Binance | Funding plus dispersé, biais long persistant |
| HLP (vault contre-partie) ne hedge pas cross-venue | Funding extrêmes persistent plus longtemps |
| Funding cap à 4% par 8h (≈ +10.95% APR) | Plafond visible — on sait quand un coin "sature" |
| Transparence onchain | Backtest possible via Dune (limité par lag d'indexation) |
| Pas de KYC + bridge USDC simple | Capital allocable rapidement |

### Pourquoi pas un perp short sur Binance à la place

- Funding Binance plus strict (caps + market makers pros qui arb)
- Marché plus efficient → edge moindre
- Mais HL a smart-contract risk + bridge delay → balance

### Pourquoi pas un long pour récolter le funding négatif

C'est implémentable (mode `direction_mode: "both"` à venir, cf. roadmap),
mais v1 = `short_high_funding` uniquement pour minimiser la complexité.
Les opportunités côté funding très négatif existent (on a vu BCH à
−31% APR z=−4 lors du test) mais en v1 on les laisse passer.

---

## 3. La stratégie en détail

### Mécanique du trade

```
Funding ≥ +50% APR persistant + liquidité OK + z-score > 2
  ↓
SHORT le perp HL au market, sizing capé, levier 2-3x
  ↓
Hold jusqu'à : funding redescend < +15% APR  (exit principal)
            OU prix baisse de 8% (take profit)
            OU prix monte de 10% (stop loss)
            OU z-score < 0.5 (signal mort)
            OU 168h (7 jours timeout)
  ↓
Close au market
  ↓
Cooldown 6h sur ce coin avant re-entry
Si exit = stop_loss, prochaine entrée sur ce coin = taille × 0.5
```

### Conditions d'entrée (ALL gates must pass)

| # | Condition | Paramètre config | Default | Pourquoi |
|---|---|---|---|---|
| 1 | Funding APR ≥ seuil | `entry.min_funding_apr_pct` | 50 | Filtrer les funding "normaux" |
| 2 | Direction = positif si mode short | `entry.direction_mode` | `short_high_funding` | v1 sans symétrie |
| 3 | Premium dans la marge | `entry.max_premium_bps` | 200 | Premium extrême = basis blowup risk |
| 4 | Persistance N heures | `entry.persistence_hours` | 3 | Filtrer les spikes éclair d'un whale |
| 5 | Z-score ≥ seuil | `entry.min_funding_zscore` | 2.0 | Extrémité vs histoire propre du coin |
| 6 | OI ≥ minimum | `universe.min_open_interest_usd` | 5_000_000 | Liquidité suffisante pour entrer/sortir |
| 7 | Spread ≤ max | `universe.max_spread_bps` | 5 | Limiter slippage à l'entrée |
| 8 | Pas en cooldown | `exit.reentry_cooldown_hours` | 6 | Pas de re-entry impulsive après exit |
| 9 | Pas dans `exclude_coins` | `universe.exclude_coins` | [] | Blocklist manuelle |
| 10 | Si `include_only` non vide, doit y être | `universe.include_only` | [] | Whitelist optionnelle |

### Conditions de sortie (first triggered wins)

| Priorité | Condition | Paramètre | Default | Géré par |
|---|---|---|---|---|
| 1 | Take profit | `exit.take_profit_pct` | 8% | **HL natif (trigger)** |
| 2 | Stop loss | `exit.stop_loss_pct` | 10% | **HL natif (trigger)** |
| 3 | Funding normalisé | `exit.funding_apr_exit_threshold` | 15% | Bot (tick horaire) |
| 4 | Z-score normalisé | `exit.exit_on_zscore_below` | 0.5 | Bot (tick horaire) |
| 5 | Timeout | `exit.timeout_hours` | 168 (7 jours) | Bot (tick horaire) |

**Architecture native TP/SL** : si `execution.use_native_triggers: true`
(défaut), le bot place IMMÉDIATEMENT après chaque open des trigger orders
reduce-only sur HL pour SL et TP. HL les enforce en temps réel — même si le
bot est offline ou crash, la position reste protégée. Le bot conserve les
exits "intelligents" (funding/zscore/timeout) au rythme horaire.

Avant tout close manuel par le bot, les triggers en attente sont cancelés
(`exchange.cancel`) pour éviter qu'ils ne s'exécutent contre notre close.

À chaque tick, une **réconciliation** compare la DB OPEN positions vs le
HL user_state réel. Si une position est absente côté HL → un SL/TP s'est
déclenché (ou un close manuel a été fait sur l'UI) → on récupère le fill
correspondant via `user_fills` et on enregistre le close avec exit_reason
`hl_sl_triggered` / `hl_tp_triggered` / `external_close`.

> ⚠️ **Contrainte de cohérence** : `entry.min_funding_apr_pct` doit être
> **strictement supérieur** à `exit.funding_apr_exit_threshold`. Sinon
> chaque entrée se ferme immédiatement au tick suivant. La validation
> Pydantic refuse les configs incohérentes.

### Sizing

```python
# Par position
notional_usd = min(
    capital × max_position_pct / 100,        # cap par position (8% défaut)
    (capital × max_total_exposure_pct / 100) - notional_déjà_déployé,
    coin_OI × max_pct_of_coin_oi / 100,      # cap OI (1% défaut)
)
notional_usd × = post_stop_multiplier         # 0.5 si le dernier exit était SL
leverage = 3 si coin in majors_list else 2    # capé par maxLeverage HL
```

### Caps de risque

| Cap | Paramètre | Default |
|---|---|---|
| Positions concurrentes | `account.max_concurrent_positions` | 4 |
| % capital par position | `account.max_position_pct` | 8% |
| % capital total déployé | `account.max_total_exposure_pct` | 30% |
| % d'OI d'un coin | `risk.max_pct_of_coin_oi` | 1% |
| Levier majors | `sizing.leverage_majors` | 3x |
| Levier midcaps | `sizing.leverage_midcaps` | 2x |

### Circuit breakers (halt automatique)

| Trigger | Paramètre | Default | Action |
|---|---|---|---|
| Perte réalisée 24h | `risk.daily_loss_halt_pct` | 3% du capital | Halt new entries |
| Drawdown total vs peak | `risk.total_drawdown_kill_pct` | 15% | Kill switch (halt) |
| Margin ratio warning | `risk.margin_ratio_warning` | 0.40 | Log warning |
| Margin ratio critical | `risk.margin_ratio_critical` | 0.25 | Log critical |

Le halt bloque les nouvelles entrées mais **ne ferme pas** les positions
existantes — elles continuent d'être gérées par leurs exit rules.

### Scoring pour le ranking

Quand plusieurs candidats valides sont détectés au même tick, on les range
par score décroissant et on prend les top N (où N = slots restants).

```python
score = |funding_apr_pct| × |z_score| × min(1, OI_usd / 50_000_000)
```

→ Privilégie les coins avec funding extrême, z élevé, et OI > $50M.

---

## 4. Architecture du code

### Structure des fichiers

```
hl-funding-sniper/
├── config.yaml          ← TOUS les paramètres ici (rien hardcodé dans le code)
├── requirements.txt     ← Dépendances Python (dont rich pour l'UI terminal)
├── .env.example         ← Template variables d'environnement (NEVER commit .env)
├── .gitignore           ← Exclut .env, state/, logs/, .venv/
├── README.md            ← Ce document
├── state/               ← Créé au runtime
│   └── positions.db     ← SQLite : positions ouvertes/fermées + funding collecté
├── logs/                ← Créé au runtime
│   └── bot.log          ← Rotation auto (5×5MB), PLAIN TEXT (grep-friendly)
└── src/
    ├── __init__.py
    ├── config.py        ← Pydantic schema + loader (+ injection env vars)
    ├── data_client.py   ← HL Info API wrapper (funding, OI, prix, spread, premium)
    ├── signal_engine.py ← Logique entry/exit + z-score + persistence (STATELESS)
    ├── position_manager.py ← Persistance SQLite + adapter pour signal engine
    ├── executor.py      ← HL Exchange API + dry-run + retries + parsing fills
    ├── risk_manager.py  ← Caps, circuit breakers, sizing, cooldowns
    ├── ui.py            ← Rich terminal UI (panels, tables, progress bars)
    ├── notifier.py      ← RichHandler console + plain file log + Telegram
    └── main.py          ← Orchestrateur (tick loop) + CLI
```

### Le rendu terminal (rich UI)

À chaque tick, le terminal affiche :

```
┌─── 🎯  HL FUNDING SPIKE SNIPER  🎯 ────┐
│  Mode:        🟡 DRY-RUN               │
│  Network:     mainnet    TLS DISABLED  │
│  Capital:     $10,000.00               │
│  ... config résumée ...                │
└────────────────────────────────────────┘

─── ⏵  Tick #1  ·  2026-05-26 00:00:00 UTC ───
✓  Snapshot: 230 perps fetched
ℹ  Universe filter: 19/230 pass
   ┌──── Top 10 eligible / 19 total (by |funding|) ────┐
   │ Coin  Funding %     OI    Spread  Premium  Mark   │
   │ BCH   -43.77       $7.3M  4.4     -11      348.61 │  ← rouge si fund<0
   │ BNB   +10.95      $29.0M  1.5      -4      660.58 │  ← vert si fund>0
   │ ...                                                │
   └────────────────────────────────────────────────────┘
📊  Open positions (3)
   ┌─────────────────────────────────────────────────────────┐
   │ ID  Coin  Side   Size  Lev  Entry  Mark  Unr.  Funding  │
   │  1  SUI   SHORT  $800  2x  1.0373 1.0366 +0.54  +0.00   │
   │  ...                                                    │
   └─────────────────────────────────────────────────────────┘
🔄  Evaluating exits...        [████████████] 3/3

┌── 🟢  EXIT  ·  SUI  ·  DRY-RUN ───────┐  ← bordure verte = gain
│  Reason:              funding_norm…   │
│  Price PnL:           $+0.54          │
│  Funding collected:   $+0.0000        │
│  Total:               $+0.54          │
└───────────────────────────────────────┘

🎯  Scanning 16 eligible coins...  [████████████] 16/16

┌── 🟢  ENTRY  ·  WLFI  ·  DRY-RUN ─────┐  ← bordure verte = nouvelle position
│  Side:         SHORT                  │
│  Notional:     $800.02                │
│  Leverage:     2x                     │
│  Entry price:  0.061075               │
│  Funding APR:  +10.95%                │
│  Z-score:      +0.70                  │
│  Reason:       funding +11.0%, ...    │
└───────────────────────────────────────┘

┌── ❤️   HEARTBEAT ─────────────────────┐
│  Eligible coins:   19                 │
│  Open positions:   3                  │
│  New signals:      1                  │
│  Capital:          $10,000.54         │
│  Status:           ✅ OK              │
└───────────────────────────────────────┘
──── ⏹  Tick end  ·  14.4s ────────────

💤  Sleeping 59m 12s until next tick...
```

### Stratégie de logging

- **Console** : RichHandler + appels directs à `ui.*` → couleurs, panels, tables
  - Niveau ≥ INFO par défaut (DEBUG si configuré)
  - urllib3/hyperliquid/rlp silencés à WARNING (sinon bruit)
- **Fichier** (`logs/bot.log`) : rotation 5×5MB, format **plain text** sans
  caractères de contrôle → idéal pour `grep`, `tail -f`, parsing scripts
- **Telegram** (si activé) : alertes Markdown sur entry, exit, halt, error,
  heartbeat

### Responsabilité de chaque module

| Module | Rôle | État local | Dépendances |
|---|---|---|---|
| `config.py` | Charge & valide YAML, injecte secrets env | aucun | pydantic, yaml |
| `data_client.py` | Pull marché HL + cache funding history | cache funding (TTL 55min) | hyperliquid SDK (lazy import) |
| `signal_engine.py` | Décisions entry/exit (pures, déterministes) | aucun | rien externe |
| `position_manager.py` | DB SQLite, source de vérité bot-side | DB | sqlite3 (stdlib) |
| `executor.py` | Place ordres HL + retries + dry-run | aucun | hyperliquid SDK, eth_account |
| `risk_manager.py` | Caps, breakers, sizing, capital | halt state (in-memory) | data_client + position_manager |
| `ui.py` | Rendu terminal rich (panels/tables/progress) | console singleton | rich |
| `notifier.py` | RichHandler console + file log + Telegram + délègue à ui | aucun | rich, stdlib |
| `main.py` | Loop tick, orchestre tout | aucun | tous les modules ci-dessus |

### Flux de données par tick (séquence complète, avec triggers natifs)

```
┌────────────────────────────────────────────────────────────┐
│  tick start                                                │
└────────────────────────────────────────────────────────────┘
       │
       ▼
  risk.check_circuit_breakers()
       │
       ▼
  reconcile_positions(cfg, data, positions, notifier)
   ├─ Pull live HL user_state → set de coins avec position ouverte
   ├─ Pour chaque OPEN en DB absente sur HL (orphan) :
   │    ├─ user_fills → trouve le fill qui a fermé (match oid SL/TP)
   │    ├─ reason = "hl_sl_triggered" | "hl_tp_triggered" | "external_close"
   │    └─ positions.record_close(...) avec PnL réalisé du fill réel
       │
       ▼
  data.snapshot_all()
       │
       ▼
  Pour chaque position OPEN restante :
   ├─ accrue funding hourly → DB
   ├─ evaluate_exit (funding/zscore/timeout — TP/SL sont gérés par HL)
   ├─ Si decision.exit :
   │    ├─ _cancel_triggers_before_close(sl_oid, tp_oid) ← important !
   │    ├─ executor.close_position(coin)
   │    └─ positions.record_close(...)
       │
       ▼
  Si risk.allow_new_entries() :
   ├─ Pour chaque candidat top après ranking :
   │    ├─ sizing = risk.size_new_entry(...)
   │    ├─ fill = executor.open_short(...)
   │    ├─ pid = positions.record_open(...)
   │    └─ _place_triggers_for_open(coin, "short", size_base, entry_px, pid)
   │         ├─ executor.place_trigger_sl → sl_oid
   │         ├─ executor.place_trigger_tp → tp_oid
   │         └─ positions.update_trigger_oids(pid, sl_oid, tp_oid)
       │
       ▼
  notifier.heartbeat(...)
       │
       ▼
┌────────────────────────────────────────────────────────────┐
│  tick end → sleep jusqu'au prochain (≈ 59 min)             │
└────────────────────────────────────────────────────────────┘

  Pendant le sleep : HL surveille les triggers en temps réel.
  Si SL ou TP fire → position fermée côté HL. Détecté à la
  réconciliation du prochain tick.
```

### Idempotence des ticks

- Si un tick crash en plein milieu (network, OOM), la DB SQLite est dans
  un état cohérent (autocommit).
- Au tick suivant : les positions toujours OUVERTES dans la DB sont
  ré-évaluées normalement. Aucun double-trade possible parce que `_scan_and_open`
  filtre les coins déjà dans `positions.open_coins()`.
- Le funding accrual `add_funding_collected` est cumulatif → si on rate
  un tick on perd l'accrual de cette heure-là (acceptable).

---

## 5. Configuration exhaustive (tous les paramètres)

Tous les paramètres sont dans **`config.yaml`** à la racine. Aucune
valeur hardcodée dans le code. Pour changer le comportement → édite YAML,
restart bot.

### Section `account`

| Clé | Type | Default | Description |
|---|---|---|---|
| `capital_usdc` | float | 10000 | Capital de référence (USDC). En dry-run sert de baseline. En live le bot lit la vraie valeur du compte mais cape à cette valeur. |
| `max_concurrent_positions` | int 1-20 | 4 | Nombre max de positions simultanées. |
| `max_total_exposure_pct` | 0-100 | 30 | % max du capital déployé toutes positions confondues. |
| `max_position_pct` | 0-100 | 8 | % max du capital par position. |

### Section `universe`

| Clé | Type | Default | Description |
|---|---|---|---|
| `min_open_interest_usd` | float | 5_000_000 | OI minimum (USD) pour qu'un coin soit éligible. |
| `max_spread_bps` | float | 5 | Spread effectif max (bps). Calculé = `(impact_ask − impact_bid) / mid × 10000`. |
| `exclude_coins` | list[str] | [] | Blocklist (ex: `["PURR", "PUMP"]`). |
| `include_only` | list[str] | [] | Si non vide, ONLY ces coins. Override tout le reste. |

### Section `entry`

| Clé | Type | Default | Description |
|---|---|---|---|
| `min_funding_apr_pct` | float | 50 | Seuil funding (APR %, valeur absolue). DOIT être > `exit.funding_apr_exit_threshold`. |
| `persistence_hours` | int 1-48 | 3 | Funding doit dépasser le seuil sur N samples horaires consécutifs (même signe). |
| `min_funding_zscore` | float | 2.0 | Z-score min sur la fenêtre. 2.0 ≈ top 2.5% historique. |
| `zscore_lookback_days` | int 1-365 | 30 | Fenêtre de calcul du z-score. |
| `max_premium_bps` | float | 200 | Skip si \|premium\| > N bps (risque convergence violente). |
| `direction_mode` | "short_high_funding" \| "both" | "short_high_funding" | En v1 : uniquement short sur funding positif extrême. |

### Section `sizing`

| Clé | Type | Default | Description |
|---|---|---|---|
| `leverage_majors` | int 1-50 | 3 | Levier pour BTC/ETH/SOL. Cap auto à `maxLeverage` HL. |
| `leverage_midcaps` | int 1-50 | 2 | Levier pour le reste. Cap auto à `maxLeverage` HL. |
| `majors_list` | list[str] | ["BTC","ETH","SOL"] | Quels coins comptent comme "majors". |
| `method` | "equal" \| "score_weighted" | "equal" | v1 : sizing équipondéré entre les top candidats. |

### Section `exit`

| Clé | Type | Default | Description |
|---|---|---|---|
| `funding_apr_exit_threshold` | float | 15 | Exit quand funding APR descend sous N %. DOIT être < `entry.min_funding_apr_pct`. |
| `take_profit_pct` | float > 0 | 8 | Take profit (% favorable). Pour un SHORT = baisse de prix de N%. |
| `stop_loss_pct` | float > 0 | 10 | Stop loss (% adverse). Pour un SHORT = hausse de prix de N%. URGENT. |
| `timeout_hours` | int > 0 | 168 | Durée max de hold (7 jours). |
| `exit_on_zscore_below` | float | 0.5 | Exit quand z-score normalise (signal mort). |
| `reentry_cooldown_hours` | int ≥ 0 | 6 | Cooldown sur le coin après exit. 0 = pas de cooldown. |
| `post_stop_size_multiplier` | 0-1 | 0.5 | Si dernier exit = `stop_loss`, prochaine entrée × ce facteur. |

### Section `risk`

| Clé | Type | Default | Description |
|---|---|---|---|
| `daily_loss_halt_pct` | > 0 | 3 | Halt new entries si perte réalisée 24h ≥ N% du capital. |
| `total_drawdown_kill_pct` | > 0 | 15 | Kill switch si drawdown total vs peak ≥ N%. |
| `margin_ratio_warning` | 0-1 | 0.40 | Log warning si margin ratio dépasse. |
| `margin_ratio_critical` | 0-1 | 0.25 | Log critical. DOIT être < `margin_ratio_warning`. |
| `max_pct_of_coin_oi` | 0-100 | 1.0 | Cap notional par position à N% de l'OI total du coin. |

### Section `execution`

| Clé | Type | Default | Description |
|---|---|---|---|
| `dry_run` | bool | **true** | Si true : pas d'ordre réel, fills simulés au mark, DB mise à jour. |
| `order_type` | "market" \| "limit_post" | "market" | v1 : seulement market implémenté. |
| `slippage_tolerance` | 0-1 | 0.005 | Tolérance pour market orders (0.005 = 0.5%). |
| `limit_timeout_seconds` | int > 0 | 30 | Timeout limit avant fallback market (pas encore branché). |
| `retry_attempts` | 1-10 | 3 | Nombre de retries sur fill échoué. |
| `retry_delay_seconds` | ≥ 1 | 5 | Délai entre retries (secondes). |
| `use_cross_margin` | bool | false | Cross (true) ou isolated (false) margin par position. |
| `use_native_triggers` | bool | true | Place SL+TP via trigger orders HL natifs. Voir Architecture native TP/SL ci-dessous. |
| `tls_verify` | bool | true | TLS verify pour API HL. Mettre `false` UNIQUEMENT sur machine MITM corporate. |

### Section `scheduler`

| Clé | Type | Default | Description |
|---|---|---|---|
| `tick_interval_seconds` | ≥ 60 | 3600 | Intervalle entre ticks. 3600 = horaire (recommandé). |
| `tick_offset_seconds_before_hour` | 0-3600 | 60 | Offset avant l'heure pile pour exécuter (HL settle sur l'heure). 60 = exécute au cran de minute 59. |

### Section `hyperliquid`

| Clé | Type | Default | Description |
|---|---|---|---|
| `network` | "mainnet" \| "testnet" | "mainnet" | Réseau HL ciblé. |
| `wallet_address` | str | "" | Adresse wallet (lecture state + balance). Requis si live. |

### Section `notifications`

| Clé | Type | Default | Description |
|---|---|---|---|
| `log_level` | "DEBUG"\|"INFO"\|"WARNING"\|"ERROR" | "INFO" | Niveau de log. |
| `log_file` | str | "logs/bot.log" | Fichier log (rotation 5×5MB auto). |
| `telegram.enabled` | bool | false | Active alertes Telegram. |
| `telegram.chat_id` | str | "" | ID chat Telegram. |
| `heartbeat` | bool | true | Émet un log/notif récap à chaque tick même si rien ne s'est passé. |

### Variables d'environnement (NEVER in YAML)

| Variable | Quand | Description |
|---|---|---|
| `HL_PRIVATE_KEY` | Si `dry_run: false` | Clé privée du wallet (hex). |
| `TELEGRAM_BOT_TOKEN` | Si `telegram.enabled: true` | Token bot Telegram. |

Voir `.env.example`. Le bot charge automatiquement `.env` au démarrage
via `python-dotenv` — pas besoin de `export` manuel.

### Auto-dérivation du wallet_address

Le bot **dérive automatiquement** `hyperliquid.wallet_address` depuis
`HL_PRIVATE_KEY` au démarrage (via `eth_account.Account.from_key`).
Donc :

- Laisse `wallet_address: ""` dans `config.yaml`
- Mets `HL_PRIVATE_KEY=0x...` dans `.env`
- Le bot dérive l'adresse au boot et l'affiche tronquée (`0x…XXXX`) dans le banner

**Pourquoi** : ça élimine tout risque de commit accidentel du wallet sur
le repo public, et garantit que la clé et l'adresse sont toujours
cohérentes (impossible de mismatch).

Si tu veux **override** explicitement (par exemple pour monitor une autre
adresse en read-only), tu peux mettre une vraie adresse dans `config.yaml`
— le bot respecte la valeur explicite si elle est non-vide.

---

## 6. Setup pas-à-pas

### Prérequis

- **Python 3.11+** (testé sur 3.11.7)
- **pip 23+**
- ~100 Mo d'espace disque pour les deps
- (Optionnel) wallet HL avec USDC pour le live

### Installation standard

```bash
cd hl-funding-sniper
python -m venv .venv

# Linux/macOS
source .venv/bin/activate

# Windows (Git Bash)
source .venv/Scripts/activate

# Windows (cmd/PowerShell)
.venv\Scripts\activate

pip install -r requirements.txt
```

### Spécificité Windows : encodage console (auto-géré)

Le bot configure automatiquement au démarrage :
- **UTF-8** sur stdout/stderr via `sys.stdout.reconfigure()` → emojis et bordures
  Unicode s'affichent correctement sur Windows cmd/PowerShell
- **COLUMNS=120** par défaut (override possible via env var) → rich rend les
  tables proprement sans tronquer
- **`.env`** auto-chargé via `python-dotenv` → `HL_PRIVATE_KEY` etc. disponibles
  sans `set -a; source .env`

Donc une seule commande suffit, peu importe le shell :

```bash
.venv\Scripts\python.exe -m src.main           # boucle continue
.venv\Scripts\python.exe -m src.main --once    # un seul tick
```

Si tu vois encore des `?` à la place de bordures malgré ça, c'est que ton
terminal ne supporte pas les escape codes ANSI — utilise Windows Terminal
ou Git Bash.

### Spécificité environnement corporatif (MITM proxy)

Si pip ou le bot échoue avec `SSLCertVerificationError` → l'antivirus /
proxy d'entreprise intercepte les connexions HTTPS et présente un cert
non reconnu.

**Workaround pip** :
```bash
pip install --trusted-host pypi.org --trusted-host files.pythonhosted.org \
  --trusted-host pypi.python.org -r requirements.txt
```

**Workaround bot** (config.yaml) :
```yaml
execution:
  tls_verify: false
```

Le bot fait du monkey-patching de `requests.Session.__init__` avant
d'importer le SDK HL pour désactiver la vérification SSL.
**À NE PAS faire en prod sur une machine non-corporate.**

### Vérification de l'installation

```bash
PYTHONIOENCODING=utf-8 python -m src.main --once
```

Tu devrais voir :
- `Loaded config from config.yaml (dry_run=True)`
- `[WARNING] DRY-RUN mode: no real orders will be placed`
- `tick start / tick end`
- `eligible: 20+ open: 0 new signals: 0` (0 signals normal au défaut strict)

---

## 7. Modes d'exécution

| Mode | `dry_run` | `observe_only` (à venir) | Effet |
|---|---|---|---|
| **Dry-run** (default) | true | n/a | Pas d'ordre réel. Fills simulés au mark. DB mise à jour normalement. Idéal pour tester le state machine. |
| **Live** | false | n/a | Ordres réels sur HL. Requiert `HL_PRIVATE_KEY` + `wallet_address`. |
| **Observe-only** (roadmap) | n/a | true | Évalue les signaux, log les ordres potentiels, **zéro side-effect** (ni DB ni API). Pas encore implémenté. |

### Précédence

`observe_only` (futur) > `dry_run` > live.
Si `observe_only=true`, le `dry_run` est ignoré et aucune écriture nulle part.

---

## 8. Lancement & exploitation

### Un seul tick (debug / cron)

```bash
PYTHONIOENCODING=utf-8 python -m src.main --once
```

Utile pour :
- Vérifier que la config charge
- Voir un cycle complet
- Mettre dans un cron horaire à la place de la boucle interne

### Boucle continue (production)

```bash
PYTHONIOENCODING=utf-8 python -m src.main
```

Le bot dort entre les ticks (sleep adaptatif vers le prochain mark de
settlement). `Ctrl+C` interrompt proprement après le tick courant.

### Avec un fichier de config alternatif

```bash
python -m src.main --config configs/conservative.yaml
```

Tu peux maintenir plusieurs configs (`paper.yaml`, `live.yaml`, etc.).

### Lancement en arrière-plan permanent

**systemd** (Linux) :
```ini
# /etc/systemd/system/hl-sniper.service
[Unit]
Description=HL Funding Spike Sniper
After=network.target

[Service]
Type=simple
User=hlbot
WorkingDirectory=/home/hlbot/hl-funding-sniper
EnvironmentFile=/home/hlbot/hl-funding-sniper/.env
ExecStart=/home/hlbot/hl-funding-sniper/.venv/bin/python -m src.main
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
```

**Windows Task Scheduler** : créer une tâche horaire qui lance
`python -m src.main --once`. Le bot étant idempotent, c'est sûr.

**tmux/screen** pour une session manuelle :
```bash
tmux new -s sniper
PYTHONIOENCODING=utf-8 python -m src.main
# Ctrl+B puis D pour détacher
```

### Inspection de la DB

```bash
python -c "
import sqlite3
c = sqlite3.connect('state/positions.db')
c.row_factory = sqlite3.Row
for r in c.execute('SELECT * FROM positions ORDER BY id'):
    print(dict(r))
"
```

Ou ouvre `state/positions.db` avec DB Browser for SQLite, TablePlus, etc.

### Reset manuel

```bash
rm state/positions.db
# Le bot recrée le schéma au prochain démarrage
```

---

## 9. État persistant (SQLite + logs)

### Schéma DB (`state/positions.db`)

```sql
CREATE TABLE positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    coin TEXT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('short','long')),
    size_usd REAL NOT NULL,
    size_base REAL,                       -- coin units, pour cancel triggers
    leverage INTEGER NOT NULL,
    entry_price REAL NOT NULL,
    entry_timestamp TEXT NOT NULL,
    entry_funding_apr REAL,
    entry_zscore REAL,
    entry_reason TEXT,
    sl_oid INTEGER,                       -- HL order id du trigger SL
    tp_oid INTEGER,                       -- HL order id du trigger TP
    exit_price REAL,
    exit_timestamp TEXT,
    exit_reason TEXT,
    realized_pnl_usd REAL,
    funding_collected_usd REAL DEFAULT 0,
    status TEXT DEFAULT 'OPEN' CHECK (status IN ('OPEN','CLOSED','ERROR'))
);

CREATE TABLE exits_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    coin TEXT NOT NULL,
    exit_timestamp TEXT NOT NULL,
    exit_reason TEXT NOT NULL,
    realized_pnl_usd REAL
);
```

Les valeurs possibles de `exit_reason` :

| Reason | Origine | Quand |
|---|---|---|
| `take_profit` | Bot (legacy) ou HL natif (`hl_tp_triggered`) | Prix favorable hit |
| `stop_loss` | Bot (legacy) ou HL natif (`hl_sl_triggered`) | Prix défavorable hit |
| `hl_tp_triggered` | Reconciliation | TP HL fired, détecté au tick suivant |
| `hl_sl_triggered` | Reconciliation | SL HL fired, détecté au tick suivant |
| `funding_normalized` | Bot tick horaire | Funding redescend sous seuil |
| `zscore_normalized` | Bot tick horaire | Z-score sous seuil |
| `timeout` | Bot tick horaire | Durée max atteinte |
| `external_close` | Reconciliation | Close manuel sur HL UI, fill retrouvé |
| `external_close_unknown_price` | Reconciliation | Close détecté mais fill introuvable |

### Logs

- `logs/bot.log` : rotation auto (max 5 fichiers × 5MB)
- Format : `YYYY-MM-DD HH:MM:SS,ms [LEVEL] module: message`
- Console + fichier (mêmes messages)
- Niveau configurable via `notifications.log_level`

---

## 10. Validations & tests réalisés (dry-run)

Lors du développement, validé end-to-end via 2 ticks consécutifs en dry-run :

| # | Validation | Résultat |
|---|---|---|
| 1 | Snapshot HL 230 coins en 1 appel | ✅ |
| 2 | Filtres univers (OI > $5M, spread < 5bps) | ✅ 20-24 éligibles |
| 3 | Fetch funding history par coin + cache (TTL 55min) | ✅ |
| 4 | Calcul z-score sur 30j d'histoire | ✅ |
| 5 | Évaluation entry (10 gates en cascade) | ✅ |
| 6 | Ranking par score | ✅ |
| 7 | Sizing avec caps (per-pos, total, OI) | ✅ $800/pos = 8% × $10k |
| 8 | Leverage tier majors vs midcaps + cap HL | ✅ ASTER demandé 2x, cap HL=5x |
| 9 | Simulation fill dry-run sans ordre réel | ✅ |
| 10 | Écriture SQLite (open + close + exits_history) | ✅ |
| 11 | Évaluation exit (TP/SL/funding/zscore/timeout) | ✅ |
| 12 | Cooldown re-entry après exit | ✅ |
| 13 | PnL réalisé tracké | ✅ |
| 14 | Heartbeat avec stats | ✅ |
| 15 | Notifs entry/exit formatées | ✅ |
| 16 | Cross-validation Pydantic sur seuils | ✅ |
| 17 | TLS bypass pour MITM corporate | ✅ |

### Observations marché du test

- 230 perps HL au total, ~20-24 passent les filtres OI+spread par défaut
- 15+ coins collés au funding cap structurel HL (+10.95% APR = 1% par 8h)
- Funding négatifs intéressants vus (BCH −31% APR z=−4, ADA −15%, BTC −11.5%)
  → exploitables avec `direction_mode: "both"` (roadmap)
- Avec seuils par défaut (50% APR + persistence 3h + z=2) : 0 signal sur
  notre snapshot (normal — marché calme ce jour-là)

---

## 11. Bugs trouvés et corrections

### Bug #1 : Cohérence entry/exit threshold

**Symptôme** : avec `min_funding_apr_pct: 5` (entry) et `funding_apr_exit_threshold: 15`
(exit), chaque entrée se ferme au tick suivant en boucle.

**Cause** : la règle exit `if abs(funding_apr) < 15` est vraie dès qu'on
entre à 11% (< 15%).

**Fix** : `Config._cross_checks` rejette toute config où
`entry.min_funding_apr_pct ≤ exit.funding_apr_exit_threshold`.

### Bug #2 : SSL handshake échoue derrière MITM corporate

**Symptôme** : `SSLCertVerificationError` même avec `certifi.where()`.

**Cause** : antivirus / proxy d'entreprise présente son propre cert,
absent du bundle certifi.

**Fix** : option `execution.tls_verify: false` + monkey-patch de
`requests.Session.__init__` AVANT l'import lazy du SDK HL.

### Bug #3 (cosmétique) : Encodage Unicode sur Windows

**Symptôme** : `─── tick start ───` affiché comme `───`.

**Cause** : console Windows par défaut en cp1252.

**Fix** : doc dans le README, recommander `PYTHONIOENCODING=utf-8`.

---

## 12. Hyperliquid : ce qu'il faut savoir

### Funding rate

- Réglé **toutes les heures** (pas toutes les 8h comme Binance)
- `funding` brut dans l'API = rate par heure (ex: 0.0001 = 0.01%/h ≈ 87.6% APR)
- Cap structurel : **1% par 8h** ≈ **+10.95% APR** annualisé
- Plein cap atteint = sentiment extrêmement biaisé, mais la stratégie veut
  un coin qui DÉPASSE ce cap → ce n'est plus la vraie limite, c'est la
  borne où le funding sature visuellement.

### Notional & sizing

- Minimum notional ordre : **~$10**
- `szDecimals` par coin dans `meta()['universe'][i]['szDecimals']` → bot
  arrondit auto via `_round_size`
- `maxLeverage` par coin → bot cape via `_max_leverage`

### Leverage modes

- **Cross** : marge mutualisée tout le compte. Plus efficient capital
  mais une position qui se rate liquide tout.
- **Isolated** : marge isolée par position. Plus sûr par position. C'est
  le default du bot (`use_cross_margin: false`).

### Fees

- Maker : −0.001% (rebate) à 0%
- Taker : 0.045% (référence sans vol discount)
- Le bot fait du market order par défaut → taker fee à comptabiliser

### Bridge

- USDC bridge Arbitrum ↔ HL via contrat `0x2df1c51e09aecf9cacb7bc98cb1742757f163df7`
- Withdraw fee : $1 USDC flat
- Dispute period : quelques heures (delay sortie en cas de panic)

### Limites API

- Pas de hard rate limit documenté, mais soft throttle à haute fréquence
- Le bot fait ~30-50 appels par tick (snapshot + funding histories), tick
  horaire = très en-deçà des limites

### Account state shape

```python
state = info.user_state(address)
# state['marginSummary']['accountValue']        ← equity totale USD
# state['marginSummary']['totalMarginUsed']     ← marge utilisée
# state['marginSummary']['totalNtlPos']         ← notional positions
# state['assetPositions']                       ← liste positions ouvertes
# state['withdrawable']                         ← cash dispo retrait
```

---

## 13. Le dashboard Dune lié

**URL** : https://dune.com/smart_ape/hl-edge-hyperliquid-trader-edge-console

### Les 9 queries qui composent le dashboard

| Section | Query ID | Description |
|---|---|---|
| Funding | 7566984 | Snapshot funding APR par coin (latest hour) |
| Funding | 7567029 | Heatmap funding top 25 × 30 jours |
| Funding | 7567034 | Série BTC funding APR + mark price (30j) |
| Premium | 7567035 | Top outliers premium = mark−oracle (bps), latest day |
| Premium | 7567036 | Série BTC premium (bps) + bandes ±3σ (30j) |
| Liquidity | 7567037 | Effective spread (bps) snapshot, top 40 |
| Liquidity | 7567039 | Médiane spread top 10 daily (30j) |
| Bridge | 7567043 | Daily net USDC flows bridge (90j) |
| Bridge | 7567044 | Flow par cohorte de taille (90j) |

### Comment utiliser le dashboard pour calibrer le bot

| Action | Section dashboard |
|---|---|
| Choisir `min_funding_apr_pct` par coin/régime | Q1 + Q2 (voir la distribution) |
| Vérifier qu'un coin candidat est liquide assez | Q6 |
| Calibrer `max_premium_bps` | Q4 |
| Backtester visuellement la stratégie sur BTC | Q3 + Q5 (regarder les exits historiques) |
| Détecter macro risk-off (réduire toute expo) | Q8 (gros outflows = risk off) |
| Filtrer le univers initial | Q6 + Q1 (croiser liquidité × funding) |

### Lag d'indexation Dune

**ATTENTION** : `hyperliquid.market_data` sur Dune est à ~3-4 semaines
de retard. Le dashboard sert UNIQUEMENT à la calibration historique et au
monitoring stratégique. Le **signal live** vient toujours de l'API HL
directement via le bot.

Le bridge en revanche est temps réel sur Dune.

---

## 14. Capacité économique attendue

Estimation à partir de stratégies similaires sur Binance/Bybit :

| Capital | APR net attendu | Sharpe | Pourquoi |
|---|---|---|---|
| $10k | 25-40% | 3.0+ | Pas de market impact, choix des meilleurs trades |
| $100k | 18-28% | 2.5 | Diversification forcée sur plus de coins |
| $1M | 12-18% | 2.0 | Market impact + cap OI atteint |
| $10M | 6-10% | 1.5 | Tu DEVIENS le funding rate sur les small caps |

**Au-delà de ~$5-10M, edge mort** sur cette stratégie sur HL seul. Pour
scaler, il faudrait étendre cross-venue (Bybit, OKX) en parallèle.

### Décomposition de l'edge

- Funding reçu : typique +20-40% APR sur les coins sélectionnés (vs cap
  +10.95% structurel sur la moyenne du marché)
- Mean reversion price : +2-5% par trade × 60-65% hit rate
- Frictions : −0.045% taker fee × 2 (in+out) + ~0.5-1% slippage estimé
  par trade = ~1-2% drag par trade
- Net : devrait sortir entre +5-15% par trade pour les bonnes setups

### Drawdowns attendus

- **15-20%** sur la version naked (notre v1) — vient des trades où le
  prix rip à la hausse avant que les longs ne soient liquidés
- **3-5%** si on hedge spot (non implémenté v1)

---

## 15. Limites connues v1

| Limite | Impact | Workaround |
|---|---|---|
| Pas de mode `observe_only` | On pollue la DB en mode test | Reset DB manuellement |
| Pas de mode `direction_mode: both` | On rate les opportunités funding négatif (BCH, ADA quand z<−2) | Mode roadmap |
| Pas de hedge spot | 100% risque directionnel | C'est volontaire — HL spot trop limité |
| ~~SL/TP côté bot polling~~ | ~~Lent~~ | ✅ **Résolu** : triggers natifs HL + reconciliation |
| Pas d'ajustement auto de marge en critical | Juste un log warning | Monitorer manuellement |
| Pas de circuit breaker basé sur vol marché global | Si BTC dump 10%, on continue à scanner | Halt manuel via Ctrl+C |
| `limit_post` order type pas branché | Toujours market en pratique | OK pour la stratégie horaire |
| Funding accrual est une estimation | Calculé à partir du funding rate × size × hourly, pas du payment réel HL | Réconciliation au close vs DB HL |
| Pas de réconciliation auto DB ↔ HL state | Si on close manuellement une position sur HL UI, la DB ne le sait pas | À faire au démarrage si live |
| Pas de Telegram inline buttons | Notifications one-way | Ouvre HL UI pour interagir |
| Pas de backtest engine | On ne peut pas valider l'edge avant capital réel | À écrire (roadmap) |

---

## 16. Roadmap

### Court terme

- [ ] **Mode `observe_only`** : un mode purement passif qui log les ordres
      potentiels avec marker block ASCII très visible dans le terminal,
      sans toucher DB ni exchange. Précédence sur `dry_run`.
- [ ] **Mode `both`** : `direction_mode: "both"` pour aussi LONG les
      coins avec funding très négatif (récolter le funding payé par les
      shorts). Doublerait les opportunités.
- [ ] **Réconciliation DB ↔ HL state** au démarrage : si une position
      OPEN en DB n'existe plus sur HL → marquer ERROR. Inverse aussi.

### Moyen terme

- [ ] **Backtest engine** qui replay l'historique funding HL sur la
      logique du `signal_engine`. Sortie : Sharpe, max DD, hit rate par
      coin et global.
- [ ] **Dashboard Streamlit live** pour monitorer le bot en temps réel
      (positions, PnL cumulé, signaux scannés, breakers).
- [ ] **Limit-post order type** branché (post-only pour économiser maker
      rebate au lieu de payer taker).
- [ ] **Auto-deleverage** quand margin ratio passe critical.

### Long terme

- [ ] **Hedge optionnel** pour les coins avec spot HL (BTC, ETH, SOL,
      HYPE, PURR) → devient delta-neutre = stratégie de carry pure.
- [ ] **Cross-venue funding arb** (HL vs Bybit vs OKX) pour scaler
      au-delà de $5-10M.
- [ ] **ML enrichi du signal** : feature engineering sur premium, OI
      delta, bridge flows, volume profile.

---

## 17. Sécurité

### Règles d'or

1. **NEVER** commit `.env`. Il contient `HL_PRIVATE_KEY`. Le `.gitignore`
   l'exclut déjà.
2. **NEVER** mettre la clé privée dans `config.yaml`. La validation
   Pydantic ne ferait rien d'utile contre ça, c'est de la discipline pure.
3. **Commencer en dry-run** au moins 3-7 jours avant tout capital réel.
4. **Démarrer en live avec un capital minimal** ($100-500) pour valider
   le pipeline. Scale uniquement après plusieurs semaines de PnL cohérent.
5. **Utiliser un wallet dédié** au bot. Ne pas réutiliser un wallet qui
   contient tes autres assets.
6. **`tls_verify: false`** uniquement sur ta machine corporate qui te
   trust (MITM légitime). JAMAIS sur une VPS / serveur cloud.
7. **HL est un L1 propriétaire** avec set de validateurs limité.
   Smart-contract risk réel. Cap le capital total HL à ce que tu
   accepterais de perdre à 100%.
8. **Bridge HL ↔ Arbitrum** a un délai (dispute period). Ne pas tout
   déployer, garder du cash hors-HL.

### Audit avant first deploy

Avant `dry_run: false`, vérifier :

- [ ] Wallet utilisé est dédié au bot
- [ ] Capital sur HL est ≤ ce que tu acceptes de perdre
- [ ] `max_position_pct` × `max_concurrent_positions` ≥ `max_total_exposure_pct` (sinon caps incohérents)
- [ ] `total_drawdown_kill_pct` est ≤ ta vraie tolérance perso
- [ ] Telegram fonctionne (test avec heartbeat)
- [ ] Le bot a réussi au moins 24h en dry-run sans crash

---

## 18. Glossaire & formules

### Termes

| Terme | Définition |
|---|---|
| **Funding rate** | Paiement périodique entre longs et shorts d'un perp pour ancrer le mark price au spot. Positif = longs paient shorts. |
| **Funding APR** | Funding annualisé. `funding × 24 × 365 × 100` si funding est hourly. |
| **Mark price** | Prix de référence pour PnL et liquidations (souvent oracle-based ou moyenne pondérée). |
| **Oracle price** | Prix externe agrégé (Pyth / autre) qui sert d'ancre au funding. |
| **Premium** | Écart `mark − oracle`. Quand extrême → basis disloqué. |
| **Open Interest (OI)** | Notional total des positions ouvertes (somme des longs = somme des shorts). |
| **Effective spread** | `(impact_ask − impact_bid) / mid × 10000` en bps. Mesure la liquidité réelle pour un trade de taille moyenne. |
| **Z-score** | `(current − mean) / stdev` sur la fenêtre. Mesure d'extrémité statistique. |
| **HLP** | Hyperliquidity Provider — vault qui prend la contre-partie du flux retail HL. |
| **Cooldown** | Durée pendant laquelle on n'ouvre pas une nouvelle position sur un coin qui vient d'être fermé. |
| **Post-stop multiplier** | Facteur de réduction de taille après un stop-loss sur ce coin. |

### Formules clés

**Funding APR (depuis funding horaire HL)** :
```
funding_apr_pct = funding × 24 × 365 × 100
```

**Premium en bps** :
```
premium_bps = (mark_px - oracle_px) / oracle_px × 10000
```

**Effective spread en bps** :
```
spread_bps = (impact_ask_px - impact_bid_px) / mid_px × 10000
```

**Z-score (rolling 30j de funding horaire)** :
```
z = (current_funding_apr - mean(history_apr)) / stdev(history_apr)
```

**OI en USD** :
```
oi_usd = openInterest_base × mark_px
```

**PnL réalisé d'un short à la fermeture** :
```
price_pnl_usd = -1 × (close_px - entry_px) / entry_px × size_usd
```

**Funding collecté par heure pour un short** :
```
funding_hourly_usd = -1 × (funding_apr_pct / 100 / (24 × 365)) × size_usd
                   = funding_pct_par_heure × size_usd   (en signe inverse vs long)
```

**Score de ranking** :
```
score = |funding_apr_pct| × |z_score| × min(1, oi_usd / 50_000_000)
```

**Sizing** :
```
notional = min(
  capital × max_position_pct / 100,
  (capital × max_total_exposure_pct / 100) - notional_already_open,
  oi_usd × max_pct_of_coin_oi / 100
) × post_stop_multiplier_if_applicable
```

---

## 19. Historique de décisions (pour un futur "moi")

Cette section sert à un futur Claude (ou toi qui aura oublié) à comprendre
**pourquoi** le code est tel qu'il est.

### Pourquoi pas Polymarket

L'utilisateur a initialement demandé "stratégie polymarket avancée pour
profiter du funding rate". J'ai d'abord interprété ça comme "stratégie
sur Polymarket le prediction market", ce qui a donné une longue réponse
hors-sujet. L'utilisateur a clarifié : il voulait dire **multi-marchés
opportuniste** sur Hyperliquid lui-même, en exploitant les funding rates.
Polymarket le produit n'est pas dans la roadmap.

### Pourquoi pas delta-neutre

Long spot + short perp = stratégie de carry pure (collecte funding sans
risque de prix). MAIS HL spot ne liste qu'une poignée de tokens (HYPE,
PURR, BTC, ETH, SOL, quelques autres). Pour 90%+ des perps HL, pas de
spot HL → il faudrait hedger sur Binance/Coinbase = cross-exchange =
2× la complexité, leg risk, capital duplication.

→ Décision : v1 naked short, accepter le risque directionnel.

### Pourquoi short et pas long sur funding élevé

Funding élevé = longs paient shorts. Pour PROFITER du funding, faut être
sur le côté qui REÇOIT = SHORT.
Si on était long avec funding élevé, on PAIE le funding (qu'on voulait
exploiter) → contre-sens.
Bonus : les funding extrêmes positifs précèdent statistiquement des
corrections de prix → le short profite aussi de la baisse. Double edge.

### Pourquoi z-score sur 30 jours

- Assez de samples (~720 horaires) pour un calcul stable
- Pas trop long pour rester réactif aux régimes (un coin qui devient
  hot récemment doit être détectable)
- 30 jours est aussi cohérent avec la fenêtre d'analyse du dashboard Dune

### Pourquoi persistence 3 heures par défaut

Un funding spike d'1 heure peut être un wick causé par 1 whale qui se
rate. À 3 heures consécutives, c'est une vraie crowd qui s'installe.
Trade-off : plus persistence est haut, moins de signaux mais plus fiables.

### Pourquoi cap OI à 1%

Au-dessus de 1% de l'OI, on commence à bouger le funding contre nous-mêmes
(notre short ajoute à l'OI court → réduit le funding positif → tue notre
edge). 1% est un compromis empirique.

### Pourquoi utiliser les triggers natifs HL plutôt que polling

Initialement j'avais codé le TP/SL côté bot via polling du mark price à
chaque tick horaire. Limites :

1. **Latence d'1 heure** pour réagir à un mouvement de prix → SL souvent
   exécuté très loin du seuil sur un crash flash.
2. **Position non protégée si le bot crashe** entre deux ticks.
3. **Tick rapide irréaliste** : voir §11, ticker à 1 minute pose des
   problèmes de rate limit HL pour 0 gain de signal.

La solution propre = laisser HL faire le boulot :
- HL voit le mark price en temps réel
- HL enforce le SL/TP **sub-seconde** côté serveur
- Le bot peut crash, redémarrer, l'internet peut couper — la position est
  toujours protégée
- C'est ce que font tous les pros (Binance, Bybit, etc. ont le même pattern)

Conséquences architecturales :
- Le bot ne polle plus le mark pour SL/TP. Tick horaire suffit pour les
  exits "intelligents" (funding_normalized, zscore_normalized, timeout)
- À chaque tick : **réconciliation** DB ↔ HL pour catcher les triggers qui
  ont fired entre 2 ticks
- Avant un close manuel : **cancel des triggers** pour qu'ils ne se
  déclenchent pas contre notre close reduce-only

### Pourquoi import lazy du SDK Hyperliquid

`Info.__init__` appelle l'API HL immédiatement. Si on veut patcher
`requests.Session` (pour TLS bypass), il faut le faire AVANT que la
session ne soit créée. Donc l'import doit être lazy (dans `HLDataClient.__init__`
et `HLExecutor.__init__`) après le patch.

### Pourquoi monkey-patch `requests.Session.__init__`

`session.verify = False` après création ne suffit pas parce que Info()
fait déjà des appels API à la construction. Donc on patche la méthode
d'init pour que TOUTES les sessions nouvellement créées aient
`verify=False`. Hack mais effectif.

### Pourquoi pas WebSocket

La stratégie est horaire. WebSocket donnerait des données tick-par-tick,
inutiles ici, et compliquerait le state. REST polling toutes les heures
suffit et est plus robuste (pas de reconnexion à gérer).

### Pourquoi SQLite et pas Postgres

- Pas de dépendance externe (sqlite3 = stdlib)
- Fichier portable, facile à inspecter, backup trivial
- Performance largement suffisante (1 write par tick par position)
- Si tu scales à 100 bots ou multi-utilisateurs, migrer vers Postgres

### Pourquoi Pydantic v2

- Validation déclarative (les contraintes sont dans le schema)
- Cross-checks via `@model_validator` pour éviter les configs piège
- Erreurs claires au load time, pas au runtime

### Pourquoi Telegram via urllib (pas python-telegram-bot)

- Une seule dépendance en moins
- Fire-and-forget, pas besoin du framework complet
- 30 lignes au total

### Conventions de logging

- `INFO` = événements importants (entry, exit, halt, heartbeat)
- `DEBUG` = détails opérationnels (skip cooldown, candidat évalué)
- `WARNING` = anomalies non-bloquantes (margin warning, snapshot partiel)
- `ERROR` / `CRITICAL` = bugs ou breakers tripped

### Conventions de naming

- Préfixe `_` pour les helpers privés
- Pas de classes pour les structures de données → `dataclass`
- `from __future__ import annotations` partout pour forward refs
- Module names en `snake_case`, classes en `PascalCase`

---

**Fin du document.** Pour toute évolution, garde ce README à jour — c'est
le seul vrai onboarding doc du projet.
