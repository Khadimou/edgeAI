"use client";

import { useQuery } from "@tanstack/react-query";
import { formatDistanceToNow } from "date-fns";
import { fr } from "date-fns/locale";
import {
  Settings, Database, Brain, Clock, Zap, AlertTriangle,
  CheckCircle2, Lock, Unlock, Trophy, RefreshCw,
} from "lucide-react";
import { adminApi } from "@/lib/api";
import { cn } from "@/lib/utils";

interface ObservabilityData {
  computed_at: string;
  environment?: string;
  pipeline_last_run?: string | null;
  wc_freshness?: {
    scheduled_matches: number;
    with_h2h_odds: number;
    with_ah_odds: number;
    predictions: number;
  };
  nba_freshness?: {
    scheduled_matches: number;
    with_moneyline_odds: number;
    with_totals_odds: number;
  };
  wc_status?: {
    x12_model: string | null;
    x12_loaded: boolean;
    goals_model_loaded: boolean;
    goals_n_teams: number;
    goals_trained_through: string | null;
    goals_home_adv: number | null;
    goals_rho: number | null;
    updated_at: string;
  } | null;
  live_perf?: {
    n: number;
    window_days: number;
    accuracy?: number;
    log_loss?: number;
    brier_score?: number;
    ou?: { n: number; pred_over?: number; actual_over?: number; calib_gap?: number };
    ah?: { n: number; accuracy?: number; pred_home?: number; actual_home?: number; calib_gap?: number };
  };
  db_stats: {
    matches_total: number;
    matches_breakdown: Array<{ sport: string; status: string; count: number }>;
    predictions_total: number;
    bets_by_status: Array<{ status: string; count: number }>;
    last_match_update: string | null;
    last_prediction_at: string | null;
  };
  deployed_models: Array<{
    version: string;
    accuracy: number;
    log_loss: number;
    brier_score: number;
    features_hash: string;
    artifact_path: string;
    trained_at: string | null;
    deployed_at: string | null;
  }>;
  standings_cache: Record<string, { cached: boolean; ttl_seconds: number | null; ttl_hours: number | null }>;
  locks: Record<string, { active: boolean; ttl_seconds: number; ttl_hours: number }>;
  odds_api_remaining: number | null;
  foot_freshness: {
    scheduled_matches: number;
    with_h2h_odds: number;
    with_ou_odds: number;
    with_ah_odds: number;
    last_update: string | null;
  };
  drift: {
    deployed_model: string;
    oof_accuracy: number;
    oof_log_loss: number;
    live_n_settled: number;
    ready_to_evaluate: boolean;
  } | null;
  whitelists: {
    value_bet_1x2_leagues: string[];
    value_bet_ou_leagues: string[];
    value_bet_ah_leagues: string[];
    per_league_model_leagues: string[];
  };
}

function Card({ children, className }: { children: React.ReactNode; className?: string }) {
  return (
    <div className={cn("bg-gray-900 border border-gray-800 rounded-xl p-5", className)}>
      {children}
    </div>
  );
}

function MetricBox({ label, value, sub, accent = "default" }: {
  label: string; value: string; sub?: string;
  accent?: "default" | "good" | "warn" | "bad";
}) {
  const color = {
    default: "text-white",
    good: "text-green-400",
    warn: "text-yellow-400",
    bad: "text-red-400",
  }[accent];
  return (
    <div className="bg-gray-800/40 rounded-lg p-3">
      <p className="text-xs text-gray-500 mb-1">{label}</p>
      <p className={cn("text-lg font-bold", color)}>{value}</p>
      {sub && <p className="text-[10px] text-gray-500 mt-0.5">{sub}</p>}
    </div>
  );
}

function timeAgo(iso: string | null): string {
  if (!iso) return "—";
  try {
    return formatDistanceToNow(new Date(iso), { addSuffix: true, locale: fr });
  } catch {
    return "—";
  }
}

export default function AdminPage() {
  const { data, isLoading, refetch, isRefetching } = useQuery<ObservabilityData>({
    queryKey: ["admin-observability"],
    queryFn: () => adminApi.observability().then((r) => r.data),
    refetchInterval: 30_000,
  });

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin w-8 h-8 border-2 border-brand-500 border-t-transparent rounded-full" />
      </div>
    );
  }

  if (!data) return null;

  const credits = data.odds_api_remaining;
  const creditsAccent: "good" | "warn" | "bad" =
    credits === null ? "warn" : credits >= 200 ? "good" : credits >= 50 ? "warn" : "bad";

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-2xl font-bold flex items-center gap-2">
            <Settings className="w-6 h-6 text-brand-500" />
            Observabilité
          </h1>
          <p className="text-sm text-gray-400 mt-1">
            Vue d'ensemble système — refresh auto 30s · dernière màj {timeAgo(data.computed_at)}
          </p>
        </div>
        <button
          onClick={() => refetch()}
          className="flex items-center gap-2 text-sm bg-gray-800 hover:bg-gray-700 px-3 py-1.5 rounded-lg"
        >
          <RefreshCw className={cn("w-3.5 h-3.5", isRefetching && "animate-spin")} />
          Rafraîchir
        </button>
      </div>

      {/* Bandeau santé pipeline */}
      {(() => {
        const isProd = data.environment === "production";
        // Santé = le pipeline a tourné récemment (heartbeat), PAS "des prédictions
        // ont été générées" : en fin de saison il peut n'y avoir aucun match à prédire.
        const lastRun = data.pipeline_last_run ?? null;
        const lastPred = data.db_stats.last_prediction_at;
        const hoursSince = lastRun ? (Date.now() - new Date(lastRun).getTime()) / 3_600_000 : null;
        // Cycle 6h → sain si dernier run < 8h. Si pas de heartbeat (déploiement récent), neutre.
        const noHeartbeat = lastRun === null;
        const stale = hoursSince !== null && hoursSince > 8;
        const healthy = isProd && !stale && !noHeartbeat;
        const cls = healthy ? "border-green-500/30 bg-green-500/10" : "border-yellow-500/30 bg-yellow-500/10";
        return (
          <div className={cn("rounded-xl border p-4 flex items-start gap-3", cls)}>
            {healthy
              ? <CheckCircle2 className="w-5 h-5 text-green-400 shrink-0 mt-0.5" />
              : <AlertTriangle className="w-5 h-5 text-yellow-400 shrink-0 mt-0.5" />}
            <div className="text-sm">
              {isProd ? (
                <>
                  <p className={cn("font-semibold", healthy ? "text-green-300" : "text-yellow-300")}>
                    {healthy ? "Pipeline 24/7 actif (Hetzner)"
                      : noHeartbeat ? "Pipeline — heartbeat en attente"
                      : "Pipeline en prod — cycle en retard"}
                  </p>
                  <p className="text-gray-300 mt-1">
                    Déployé sur VPS Hetzner, cycle toutes les 6h. Dernier run {timeAgo(lastRun)} · dernière prédiction {timeAgo(lastPred)}.
                    {noHeartbeat && " (Le heartbeat apparaîtra au prochain cycle après déploiement.)"}
                    {stale && " ⚠ Aucun run depuis >8h — vérifier les logs du ml_worker."}
                  </p>
                </>
              ) : (
                <>
                  <p className="font-semibold text-yellow-300">Environnement de développement</p>
                  <p className="text-gray-300 mt-1">
                    ENVIRONMENT ≠ production. Le pipeline tourne en local — il s'arrête si la machine s'éteint.
                    Dernier run {timeAgo(lastRun)} · dernière prédiction {timeAgo(lastPred)}.
                  </p>
                </>
              )}
            </div>
          </div>
        );
      })()}

      {/* Credits API + Cache */}
      <Card>
        <h2 className="text-sm font-semibold mb-4 text-gray-300 flex items-center gap-2">
          <Zap className="w-4 h-4" />
          APIs externes
        </h2>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <MetricBox
            label="the-odds-api credits"
            value={credits !== null ? `${credits}` : "—"}
            sub="reset chaque mois"
            accent={creditsAccent}
          />
          <MetricBox
            label="Foot odds locked"
            value={data.locks["foot:odds:lock"]?.active ? `${data.locks["foot:odds:lock"].ttl_hours}h` : "non"}
            sub={data.locks["foot:odds:lock"]?.active ? "prochaine fetch dans" : "next pipeline"}
            accent={data.locks["foot:odds:lock"]?.active ? "default" : "warn"}
          />
          <MetricBox
            label="NBA ingest locked"
            value={data.locks["nba:ingest:lock"]?.active ? `${data.locks["nba:ingest:lock"].ttl_hours}h` : "non"}
            sub={data.locks["nba:ingest:lock"]?.active ? "prochaine fetch dans" : "next pipeline"}
            accent={data.locks["nba:ingest:lock"]?.active ? "default" : "warn"}
          />
          <MetricBox
            label="Dernier prediction"
            value={timeAgo(data.db_stats.last_prediction_at)}
            sub="cycle 6h"
          />
        </div>
      </Card>

      {/* Cache standings */}
      <Card>
        <h2 className="text-sm font-semibold mb-4 text-gray-300">Cache standings (TTL 24h)</h2>
        <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-2">
          {Object.entries(data.standings_cache).map(([code, st]) => (
            <div key={code} className="bg-gray-800/40 rounded p-2 text-center text-xs">
              <p className="font-mono">{code}</p>
              {st.cached ? (
                <p className="text-green-400 mt-0.5 inline-flex items-center gap-1">
                  <CheckCircle2 className="w-3 h-3" /> {st.ttl_hours}h
                </p>
              ) : (
                <p className="text-red-400 mt-0.5">absent</p>
              )}
            </div>
          ))}
        </div>
      </Card>

      {/* Data freshness */}
      <Card>
        <h2 className="text-sm font-semibold mb-4 text-gray-300 flex items-center gap-2">
          <Database className="w-4 h-4" />
          Couverture cotes foot (scheduled)
        </h2>
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
          <MetricBox
            label="Matchs SCHEDULED"
            value={`${data.foot_freshness.scheduled_matches}`}
          />
          <MetricBox
            label="H2H 1X2"
            value={`${data.foot_freshness.with_h2h_odds}`}
            sub={`${data.foot_freshness.scheduled_matches > 0 ? Math.round(100 * data.foot_freshness.with_h2h_odds / data.foot_freshness.scheduled_matches) : 0}%`}
            accent="good"
          />
          <MetricBox
            label="O/U 2.5"
            value={`${data.foot_freshness.with_ou_odds}`}
            sub={`${data.foot_freshness.scheduled_matches > 0 ? Math.round(100 * data.foot_freshness.with_ou_odds / data.foot_freshness.scheduled_matches) : 0}%`}
          />
          <MetricBox
            label="Asian Handicap"
            value={`${data.foot_freshness.with_ah_odds}`}
            sub={`${data.foot_freshness.scheduled_matches > 0 ? Math.round(100 * data.foot_freshness.with_ah_odds / data.foot_freshness.scheduled_matches) : 0}%`}
          />
        </div>
      </Card>

      {/* Couverture Coupe du Monde + NBA */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <Card>
          <h2 className="text-sm font-semibold mb-4 text-gray-300 flex items-center gap-2">
            <Trophy className="w-4 h-4" />
            Coupe du Monde (scheduled)
          </h2>
          {data.wc_freshness && data.wc_freshness.scheduled_matches > 0 ? (
            <div className="grid grid-cols-2 gap-3">
              <MetricBox label="Matchs SCHEDULED" value={`${data.wc_freshness.scheduled_matches}`} />
              <MetricBox label="Prédictions" value={`${data.wc_freshness.predictions}`} accent="good" />
              <MetricBox
                label="Cotes 1X2"
                value={`${data.wc_freshness.with_h2h_odds}`}
                sub={`${Math.round(100 * data.wc_freshness.with_h2h_odds / data.wc_freshness.scheduled_matches)}%`}
              />
              <MetricBox
                label="Cotes AH"
                value={`${data.wc_freshness.with_ah_odds}`}
                sub={`${Math.round(100 * data.wc_freshness.with_ah_odds / data.wc_freshness.scheduled_matches)}%`}
              />
            </div>
          ) : (
            <p className="text-sm text-gray-500">Aucun match WC programmé (compétition inactive — démarre le 11/06).</p>
          )}
        </Card>

        <Card>
          <h2 className="text-sm font-semibold mb-4 text-gray-300 flex items-center gap-2">
            <Database className="w-4 h-4" />
            NBA (scheduled)
          </h2>
          {data.nba_freshness && data.nba_freshness.scheduled_matches > 0 ? (
            <div className="grid grid-cols-3 gap-3">
              <MetricBox label="Matchs" value={`${data.nba_freshness.scheduled_matches}`} />
              <MetricBox
                label="Moneyline"
                value={`${data.nba_freshness.with_moneyline_odds}`}
                sub={`${Math.round(100 * data.nba_freshness.with_moneyline_odds / data.nba_freshness.scheduled_matches)}%`}
              />
              <MetricBox
                label="Totals"
                value={`${data.nba_freshness.with_totals_odds}`}
                sub={`${Math.round(100 * data.nba_freshness.with_totals_odds / data.nba_freshness.scheduled_matches)}%`}
              />
            </div>
          ) : (
            <p className="text-sm text-gray-500">Aucun match NBA programmé.</p>
          )}
        </Card>
      </div>

      {/* Statut modèles Coupe du Monde */}
      <Card>
        <h2 className="text-sm font-semibold mb-4 text-gray-300 flex items-center gap-2">
          <Trophy className="w-4 h-4" />
          Modèles Coupe du Monde
        </h2>
        {data.wc_status ? (
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
            <MetricBox
              label="1X2 (XGBoost)"
              value={data.wc_status.x12_loaded ? "chargé" : "absent"}
              sub={data.wc_status.x12_model ?? "—"}
              accent={data.wc_status.x12_loaded ? "good" : "bad"}
            />
            <MetricBox
              label="Buts (Dixon-Coles)"
              value={data.wc_status.goals_model_loaded ? "chargé" : "absent"}
              sub={data.wc_status.goals_model_loaded ? `${data.wc_status.goals_n_teams} équipes` : "AH/O-U indispo"}
              accent={data.wc_status.goals_model_loaded ? "good" : "bad"}
            />
            <MetricBox
              label="Paramètres DC"
              value={data.wc_status.goals_home_adv !== null ? `γ=${data.wc_status.goals_home_adv}` : "—"}
              sub={data.wc_status.goals_rho !== null ? `ρ=${data.wc_status.goals_rho}` : ""}
            />
            <MetricBox
              label="Données jusqu'à"
              value={data.wc_status.goals_trained_through ?? "—"}
              sub={`maj ${timeAgo(data.wc_status.updated_at)}`}
            />
          </div>
        ) : (
          <p className="text-sm text-gray-500">
            Statut WC indisponible — le ml_worker n'a pas encore publié (ou modèle non chargé).
          </p>
        )}
      </Card>

      {/* Perf prédictive live */}
      {data.live_perf && data.live_perf.n > 0 && (
        <Card>
          <h2 className="text-sm font-semibold mb-1 text-gray-300 flex items-center gap-2">
            <Brain className="w-4 h-4" />
            Performance live du modèle ({data.live_perf.window_days}j)
          </h2>
          <p className="text-xs text-gray-500 mb-4">
            Qualité prédictive sur {data.live_perf.n} matchs joués — indépendant du ROI. Backfill exclu.
          </p>
          <div className="grid grid-cols-3 gap-3 mb-3">
            <MetricBox label="Accuracy 1X2" value={`${((data.live_perf.accuracy ?? 0) * 100).toFixed(1)}%`} />
            <MetricBox label="Log-loss" value={`${data.live_perf.log_loss}`} sub="bas = mieux" />
            <MetricBox label="Brier" value={`${data.live_perf.brier_score}`} sub="bas = mieux" />
          </div>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            {data.live_perf.ou && data.live_perf.ou.n > 0 && (
              <div className="bg-gray-800/40 rounded-lg p-3 text-xs">
                <p className="text-gray-500 mb-1">Calibration O/U 2.5 ({data.live_perf.ou.n})</p>
                <p>
                  prédit {((data.live_perf.ou.pred_over ?? 0) * 100).toFixed(0)}% vs réel{" "}
                  {((data.live_perf.ou.actual_over ?? 0) * 100).toFixed(0)}% —{" "}
                  <span className={cn("font-bold", Math.abs(data.live_perf.ou.calib_gap ?? 0) <= 0.05 ? "text-green-400" : Math.abs(data.live_perf.ou.calib_gap ?? 0) <= 0.10 ? "text-yellow-400" : "text-red-400")}>
                    écart {((data.live_perf.ou.calib_gap ?? 0) * 100 >= 0 ? "+" : "")}{((data.live_perf.ou.calib_gap ?? 0) * 100).toFixed(1)}pp
                  </span>
                </p>
              </div>
            )}
            {data.live_perf.ah && data.live_perf.ah.n > 0 && (
              <div className="bg-gray-800/40 rounded-lg p-3 text-xs">
                <p className="text-gray-500 mb-1">Asian Handicap ({data.live_perf.ah.n}, push exclu)</p>
                <p>
                  accuracy <span className="font-bold">{((data.live_perf.ah.accuracy ?? 0) * 100).toFixed(0)}%</span> · couv. home prédite{" "}
                  {((data.live_perf.ah.pred_home ?? 0) * 100).toFixed(0)}% vs réelle {((data.live_perf.ah.actual_home ?? 0) * 100).toFixed(0)}% —{" "}
                  <span className={cn("font-bold", Math.abs(data.live_perf.ah.calib_gap ?? 0) <= 0.05 ? "text-green-400" : Math.abs(data.live_perf.ah.calib_gap ?? 0) <= 0.10 ? "text-yellow-400" : "text-red-400")}>
                    écart {((data.live_perf.ah.calib_gap ?? 0) * 100 >= 0 ? "+" : "")}{((data.live_perf.ah.calib_gap ?? 0) * 100).toFixed(1)}pp
                  </span>
                </p>
              </div>
            )}
          </div>
        </Card>
      )}

      {/* DB stats */}
      <Card>
        <h2 className="text-sm font-semibold mb-4 text-gray-300 flex items-center gap-2">
          <Database className="w-4 h-4" />
          Base de données
        </h2>
        <div className="grid grid-cols-3 gap-3 mb-4">
          <MetricBox label="Matchs totaux" value={`${data.db_stats.matches_total}`} />
          <MetricBox label="Prédictions" value={`${data.db_stats.predictions_total}`} />
          <MetricBox label="Paris" value={`${data.db_stats.bets_by_status.reduce((s, b) => s + b.count, 0)}`} />
        </div>
        <div className="grid grid-cols-2 gap-4 text-xs">
          <div>
            <p className="text-gray-500 mb-2">Matchs par sport/statut</p>
            <table className="w-full">
              <tbody>
                {data.db_stats.matches_breakdown.map((b, i) => (
                  <tr key={i} className="border-b border-gray-800/40">
                    <td className="py-1">{b.sport}</td>
                    <td className="py-1 text-gray-400">{b.status}</td>
                    <td className="py-1 text-right font-mono">{b.count}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div>
            <p className="text-gray-500 mb-2">Paris par statut</p>
            {data.db_stats.bets_by_status.length === 0 ? (
              <p className="text-gray-600">Aucun pari</p>
            ) : (
              <table className="w-full">
                <tbody>
                  {data.db_stats.bets_by_status.map((b, i) => (
                    <tr key={i} className="border-b border-gray-800/40">
                      <td className="py-1">{b.status}</td>
                      <td className="py-1 text-right font-mono">{b.count}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </div>
      </Card>

      {/* Models déployés */}
      <Card>
        <h2 className="text-sm font-semibold mb-4 text-gray-300 flex items-center gap-2">
          <Brain className="w-4 h-4" />
          Modèles déployés ({data.deployed_models.length})
        </h2>
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead>
              <tr className="text-left text-gray-500 border-b border-gray-800">
                <th className="py-2 pr-2">Version</th>
                <th className="py-2 px-2 text-right">Accuracy</th>
                <th className="py-2 px-2 text-right">Log-loss</th>
                <th className="py-2 px-2 text-right">Brier</th>
                <th className="py-2 px-2">Déployé</th>
              </tr>
            </thead>
            <tbody>
              {data.deployed_models.map((m) => (
                <tr key={m.version} className="border-b border-gray-800/40">
                  <td className="py-1.5 pr-2 font-mono truncate max-w-[200px]">{m.version}</td>
                  <td className="py-1.5 px-2 text-right">{(m.accuracy * 100).toFixed(1)}%</td>
                  <td className="py-1.5 px-2 text-right">{m.log_loss.toFixed(3)}</td>
                  <td className="py-1.5 px-2 text-right">{m.brier_score.toFixed(3)}</td>
                  <td className="py-1.5 px-2 text-gray-400">{timeAgo(m.deployed_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>

      {/* Drift status */}
      {data.drift && (
        <Card>
          <h2 className="text-sm font-semibold mb-4 text-gray-300 flex items-center gap-2">
            <Trophy className="w-4 h-4" />
            Drift detection
          </h2>
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
            <MetricBox label="Modèle actif" value={data.drift.deployed_model.substring(0, 18) + "..."} />
            <MetricBox label="OOF accuracy" value={`${(data.drift.oof_accuracy * 100).toFixed(1)}%`} />
            <MetricBox label="OOF log-loss" value={data.drift.oof_log_loss.toFixed(3)} />
            <MetricBox
              label="Échantillon live"
              value={`${data.drift.live_n_settled}`}
              sub={data.drift.ready_to_evaluate ? "stat significatif" : "needs ≥30"}
              accent={data.drift.ready_to_evaluate ? "good" : "warn"}
            />
          </div>
        </Card>
      )}

      {/* Whitelists actives */}
      <Card>
        <h2 className="text-sm font-semibold mb-4 text-gray-300">Whitelists value betting</h2>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3 text-xs">
          <div className="bg-gray-800/40 rounded p-3">
            <p className="text-gray-500 mb-2">1X2 ({data.whitelists.value_bet_1x2_leagues.length} ligues)</p>
            <div className="flex flex-wrap gap-1">
              {data.whitelists.value_bet_1x2_leagues.map((l) => (
                <span key={l} className="px-2 py-0.5 rounded bg-brand-600/20 text-brand-300">{l}</span>
              ))}
            </div>
          </div>
          <div className="bg-gray-800/40 rounded p-3">
            <p className="text-gray-500 mb-2">O/U 2.5 ({data.whitelists.value_bet_ou_leagues.length})</p>
            <div className="flex flex-wrap gap-1">
              {data.whitelists.value_bet_ou_leagues.map((l) => (
                <span key={l} className="px-2 py-0.5 rounded bg-purple-600/20 text-purple-300">{l}</span>
              ))}
            </div>
          </div>
          <div className="bg-gray-800/40 rounded p-3">
            <p className="text-gray-500 mb-2">Asian Handicap ({data.whitelists.value_bet_ah_leagues.length})</p>
            <div className="flex flex-wrap gap-1">
              {data.whitelists.value_bet_ah_leagues.map((l) => (
                <span key={l} className="px-2 py-0.5 rounded bg-teal-600/20 text-teal-300">{l}</span>
              ))}
            </div>
          </div>
          <div className="bg-gray-800/40 rounded p-3">
            <p className="text-gray-500 mb-2">Per-league models ({data.whitelists.per_league_model_leagues.length})</p>
            <div className="flex flex-wrap gap-1">
              {data.whitelists.per_league_model_leagues.length === 0 ? (
                <span className="text-gray-600">Aucune</span>
              ) : data.whitelists.per_league_model_leagues.map((l) => (
                <span key={l} className="px-2 py-0.5 rounded bg-yellow-600/20 text-yellow-300">{l}</span>
              ))}
            </div>
          </div>
        </div>
      </Card>
    </div>
  );
}
