"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { format } from "date-fns";
import { fr } from "date-fns/locale";
import {
  LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip,
  ResponsiveContainer, ReferenceLine,
} from "recharts";
import {
  Activity, TrendingUp, TrendingDown, CheckCircle2,
  XCircle, Clock, Trophy, Target,
} from "lucide-react";
import { trackingApi, type TrackingMarket } from "@/lib/api";
import { cn, formatCurrency } from "@/lib/utils";

interface Summary {
  initial_bankroll: number;
  current_bankroll: number;
  n_pending: number;
  n_settled: number;
  n_wins: number;
  hit_rate: number;
  total_staked: number;
  total_pnl: number;
  roi_percent: number;
  max_drawdown_pct: number;
  peak_bankroll: number;
  clv_avg_percent: number | null;
  clv_positive_rate: number | null;
  clv_sample_size: number;
}

interface PerStat {
  n_bets: number;
  n_wins: number;
  hit_rate: number;
  roi_percent: number;
  pnl: number;
  stake: number;
}

interface Bet {
  match_id: string;
  match_date: string | null;
  status: string;
  sport: string;
  league: string;
  home_team: string;
  away_team: string;
  home_score: number | null;
  away_score: number | null;
  market: string;
  outcome: string;
  outcome_label: string;
  prob: number;
  odds: number;
  edge: number;
  edge_percent: number;
  stake: number;
  model_version: string;
  computed_at: string | null;
  outcome_actual: string | null;
  settled: boolean;
  won: boolean | null;
  profit: number | null;
  bankroll_after: number | null;
  opening_odds: number | null;
  clv_percent: number | null;
}

interface TrackingData {
  window_days: number;
  market_filter: string;
  summary: Summary;
  per_market: Record<string, PerStat>;
  per_league: Record<string, PerStat>;
  equity_curve: Array<{ date: string; bankroll: number }>;
  bets: Bet[];
}

const MARKET_LABELS: Record<string, string> = {
  ALL: "Tous",
  FOOTBALL_1X2: "Foot 1X2",
  FOOTBALL_OU: "Foot O/U",
  FOOTBALL_AH: "Foot AH",
  NBA: "NBA",
};

const MARKET_COLOR: Record<string, string> = {
  FOOTBALL_1X2: "bg-brand-600",
  FOOTBALL_OU: "bg-purple-600",
  FOOTBALL_AH: "bg-teal-600",
  NBA: "bg-orange-600",
};

function Kpi({ label, value, sub, accent = "default" }: {
  label: string; value: string; sub?: string;
  accent?: "default" | "good" | "bad" | "warn";
}) {
  const colorMap = {
    default: "text-white",
    good: "text-green-400",
    bad: "text-red-400",
    warn: "text-yellow-400",
  };
  return (
    <div className="bg-gray-900 border border-gray-800 rounded-xl p-4">
      <p className="text-xs text-gray-500 mb-1">{label}</p>
      <p className={cn("text-2xl font-bold", colorMap[accent])}>{value}</p>
      {sub && <p className="text-xs text-gray-500 mt-1">{sub}</p>}
    </div>
  );
}

function StatusBadge({ bet }: { bet: Bet }) {
  if (!bet.settled) {
    return (
      <span className="text-xs px-2 py-0.5 rounded-full border border-yellow-500/30 bg-yellow-500/10 text-yellow-300 inline-flex items-center gap-1">
        <Clock className="w-3 h-3" />
        En attente
      </span>
    );
  }
  if (bet.won) {
    return (
      <span className="text-xs px-2 py-0.5 rounded-full border border-green-500/30 bg-green-500/10 text-green-400 inline-flex items-center gap-1">
        <CheckCircle2 className="w-3 h-3" />
        Gagné
      </span>
    );
  }
  return (
    <span className="text-xs px-2 py-0.5 rounded-full border border-red-500/30 bg-red-500/10 text-red-400 inline-flex items-center gap-1">
      <XCircle className="w-3 h-3" />
      Perdu
    </span>
  );
}

const DAYS_OPTIONS = [
  { value: 7, label: "7j" },
  { value: 30, label: "30j" },
  { value: 60, label: "60j" },
  { value: 180, label: "180j" },
];

export default function TrackingPage() {
  const [days, setDays] = useState(60);
  const [market, setMarket] = useState<TrackingMarket>("ALL");

  const { data, isLoading } = useQuery<TrackingData>({
    queryKey: ["tracking-live", days, market],
    queryFn: () => trackingApi.live(days, market).then((r) => r.data),
    refetchInterval: 5 * 60 * 1000,
  });

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin w-8 h-8 border-2 border-brand-500 border-t-transparent rounded-full" />
      </div>
    );
  }

  if (!data) return null;

  const { summary, per_market, per_league, equity_curve, bets } = data;
  const isPositive = summary.roi_percent > 0;

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-2xl font-bold flex items-center gap-2">
            <Activity className="w-6 h-6 text-brand-500" />
            Live tracking
          </h1>
          <p className="text-sm text-gray-400 mt-1">
            Toutes les value bets identifiées par le modèle en production. Mise à jour toutes les 5 min.
          </p>
        </div>
        <div className="flex gap-2 flex-wrap">
          <div className="flex gap-1 bg-gray-900 border border-gray-800 rounded-lg p-1">
            {DAYS_OPTIONS.map((opt) => (
              <button
                key={opt.value}
                onClick={() => setDays(opt.value)}
                className={cn(
                  "px-3 py-1.5 rounded-md text-xs font-semibold transition-colors",
                  days === opt.value ? "bg-brand-600 text-white" : "text-gray-400 hover:text-gray-100"
                )}
              >
                {opt.label}
              </button>
            ))}
          </div>
          <div className="flex gap-1 bg-gray-900 border border-gray-800 rounded-lg p-1 flex-wrap">
            {(Object.keys(MARKET_LABELS) as TrackingMarket[]).map((m) => (
              <button
                key={m}
                onClick={() => setMarket(m)}
                className={cn(
                  "px-3 py-1.5 rounded-md text-xs font-semibold transition-colors",
                  market === m
                    ? (MARKET_COLOR[m] || "bg-brand-600") + " text-white"
                    : "text-gray-400 hover:text-gray-100"
                )}
              >
                {MARKET_LABELS[m]}
              </button>
            ))}
          </div>
        </div>
      </div>

      {/* KPIs principaux */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <Kpi
          label="ROI live"
          value={`${summary.roi_percent > 0 ? "+" : ""}${summary.roi_percent.toFixed(1)}%`}
          sub={`P&L: ${summary.total_pnl > 0 ? "+" : ""}${summary.total_pnl.toFixed(0)}€`}
          accent={summary.n_settled === 0 ? "default" : isPositive ? "good" : "bad"}
        />
        <Kpi
          label="Bankroll"
          value={`${summary.current_bankroll.toFixed(0)}€`}
          sub={`départ: ${summary.initial_bankroll.toFixed(0)}€`}
          accent={summary.current_bankroll >= summary.initial_bankroll ? "good" : "bad"}
        />
        <Kpi
          label="Hit rate"
          value={summary.n_settled === 0 ? "—" : `${(summary.hit_rate * 100).toFixed(1)}%`}
          sub={`${summary.n_wins}/${summary.n_settled} settled`}
        />
        <Kpi
          label="En attente"
          value={summary.n_pending.toString()}
          sub="paris à venir"
          accent="warn"
        />
      </div>

      {/* KPIs secondaires */}
      <div className="grid grid-cols-2 md:grid-cols-3 gap-4">
        <Kpi
          label="Drawdown max"
          value={summary.n_settled === 0 ? "—" : `${summary.max_drawdown_pct.toFixed(1)}%`}
          accent={summary.max_drawdown_pct > 20 ? "bad" : summary.max_drawdown_pct > 10 ? "warn" : "good"}
        />
        <Kpi
          label="Pic bankroll"
          value={`${summary.peak_bankroll.toFixed(0)}€`}
        />
        <Kpi
          label="Total misé"
          value={`${summary.total_staked.toFixed(0)}€`}
          sub={`sur ${summary.n_settled} paris settled`}
        />
      </div>

      {/* CLV — Closing Line Value */}
      <div className="bg-gray-900 border border-gray-800 rounded-xl p-5">
        <div className="flex items-start gap-3 mb-4">
          <Target className="w-5 h-5 text-brand-400 shrink-0 mt-0.5" />
          <div>
            <h2 className="text-sm font-semibold text-gray-200">
              Closing Line Value (CLV)
            </h2>
            <p className="text-xs text-gray-500 mt-0.5">
              Mesure si on identifie la value AVANT que le marché ne s'ajuste. Formule : <code>(opening / closing) - 1</code>.
              <br/>• <span className="text-green-400">CLV {'>'}  0</span> : la cote a BAISSÉ entre l'opening et le closing → le marché valide notre prédiction → on a anticipé.
              <br/>• <span className="text-red-400">CLV {'<'} 0</span> : la cote a MONTÉ → le marché s'éloigne de notre prédiction → on a fait un mauvais timing.
              <br/>Sur un échantillon ≥ 30 paris, un CLV positif moyen prouve qu'on bat le marché à long terme (KPI gold standard des pros).
            </p>
          </div>
        </div>
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
          <Kpi
            label="CLV moyen"
            value={summary.clv_avg_percent !== null
              ? `${summary.clv_avg_percent > 0 ? "+" : ""}${summary.clv_avg_percent.toFixed(2)}%`
              : "—"}
            sub={summary.clv_sample_size === 0
              ? "pas encore d'opening != closing"
              : `${summary.clv_sample_size} paris mesurés`}
            accent={
              summary.clv_avg_percent === null ? "default" :
              summary.clv_avg_percent >= 2 ? "good" :
              summary.clv_avg_percent >= 0 ? "warn" :
              "bad"
            }
          />
          <Kpi
            label="% CLV positif"
            value={summary.clv_positive_rate !== null
              ? `${(summary.clv_positive_rate * 100).toFixed(0)}%`
              : "—"}
            sub="paris où la cote a baissé"
            accent={
              summary.clv_positive_rate === null ? "default" :
              summary.clv_positive_rate >= 0.6 ? "good" :
              summary.clv_positive_rate >= 0.5 ? "warn" :
              "bad"
            }
          />
          <Kpi
            label="Échantillon"
            value={summary.clv_sample_size.toString()}
            sub={summary.clv_sample_size < 30
              ? "trop petit pour conclure"
              : "stat significative"}
            accent={summary.clv_sample_size >= 30 ? "good" : "warn"}
          />
        </div>
      </div>

      {/* Equity curve */}
      {equity_curve.length >= 2 && (
        <div className="bg-gray-900 border border-gray-800 rounded-xl p-5">
          <h2 className="text-sm font-semibold mb-4 text-gray-300 flex items-center gap-2">
            <Trophy className="w-4 h-4" />
            Courbe de bankroll (live)
          </h2>
          <div className="h-64">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={equity_curve}>
                <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
                <XAxis
                  dataKey="date" stroke="#6b7280" tick={{ fontSize: 11 }}
                  tickFormatter={(d) =>
                    new Date(d).toLocaleDateString("fr-FR", { day: "2-digit", month: "2-digit" })
                  }
                />
                <YAxis stroke="#6b7280" tick={{ fontSize: 11 }} tickFormatter={(v) => `${v}€`} />
                <Tooltip
                  contentStyle={{ backgroundColor: "#111827", border: "1px solid #1f2937", borderRadius: 8 }}
                  labelFormatter={(d) => new Date(d).toLocaleDateString("fr-FR")}
                  formatter={(val: number) => [`${val.toFixed(2)}€`, "Bankroll"]}
                />
                <ReferenceLine y={summary.initial_bankroll} stroke="#6b7280" strokeDasharray="3 3" />
                <Line
                  type="monotone"
                  dataKey="bankroll"
                  stroke={isPositive ? "#22d3ee" : "#f87171"}
                  strokeWidth={2}
                  dot={false}
                />
              </LineChart>
            </ResponsiveContainer>
          </div>
        </div>
      )}

      {/* Per market */}
      {Object.keys(per_market).length > 0 && (
        <div className="grid md:grid-cols-2 gap-4">
          <div className="bg-gray-900 border border-gray-800 rounded-xl p-5">
            <h2 className="text-sm font-semibold mb-4 text-gray-300">Par marché</h2>
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-xs text-gray-500 border-b border-gray-800">
                  <th className="py-2">Marché</th>
                  <th className="py-2 text-right">N</th>
                  <th className="py-2 text-right">Hit</th>
                  <th className="py-2 text-right">ROI</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(per_market).map(([mkt, st]) => (
                  <tr key={mkt} className="border-b border-gray-800/40">
                    <td className="py-2">{MARKET_LABELS[mkt] || mkt}</td>
                    <td className="py-2 text-right text-gray-400">{st.n_bets}</td>
                    <td className="py-2 text-right">{(st.hit_rate * 100).toFixed(1)}%</td>
                    <td className={cn(
                      "py-2 text-right font-semibold",
                      st.roi_percent >= 0 ? "text-green-400" : "text-red-400"
                    )}>
                      {st.roi_percent >= 0 ? "+" : ""}{st.roi_percent.toFixed(1)}%
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="bg-gray-900 border border-gray-800 rounded-xl p-5">
            <h2 className="text-sm font-semibold mb-4 text-gray-300">Par ligue</h2>
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-xs text-gray-500 border-b border-gray-800">
                  <th className="py-2">Ligue</th>
                  <th className="py-2 text-right">N</th>
                  <th className="py-2 text-right">Hit</th>
                  <th className="py-2 text-right">ROI</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(per_league)
                  .sort(([, a], [, b]) => b.roi_percent - a.roi_percent)
                  .map(([lg, st]) => (
                  <tr key={lg} className="border-b border-gray-800/40">
                    <td className="py-2">{lg}</td>
                    <td className="py-2 text-right text-gray-400">{st.n_bets}</td>
                    <td className="py-2 text-right">{(st.hit_rate * 100).toFixed(1)}%</td>
                    <td className={cn(
                      "py-2 text-right font-semibold",
                      st.roi_percent >= 0 ? "text-green-400" : "text-red-400"
                    )}>
                      {st.roi_percent >= 0 ? "+" : ""}{st.roi_percent.toFixed(1)}%
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Liste des paris */}
      <div className="bg-gray-900 border border-gray-800 rounded-xl p-5">
        <h2 className="text-sm font-semibold mb-4 text-gray-300 flex items-center gap-2">
          <Target className="w-4 h-4" />
          Value bets ({bets.length})
        </h2>
        {bets.length === 0 ? (
          <div className="text-center py-10 text-sm text-gray-500">
            Aucune value bet identifiée sur cette période.
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-left text-gray-500 border-b border-gray-800">
                  <th className="py-2 pr-2">Date</th>
                  <th className="py-2 px-2">Match</th>
                  <th className="py-2 px-2">Marché</th>
                  <th className="py-2 px-2">Pari sur</th>
                  <th className="py-2 px-2 text-right">Cote</th>
                  <th className="py-2 px-2 text-right">Edge</th>
                  <th className="py-2 px-2 text-right" title="Closing Line Value : cote au moment de la prédiction vs cote actuelle">CLV</th>
                  <th className="py-2 px-2 text-right">Mise</th>
                  <th className="py-2 px-2">Score</th>
                  <th className="py-2 px-2">Statut</th>
                  <th className="py-2 px-2 text-right">P&L</th>
                </tr>
              </thead>
              <tbody>
                {bets.slice(0, 100).map((b) => (
                  <tr key={`${b.match_id}-${b.market}`} className="border-b border-gray-800/40">
                    <td className="py-1.5 pr-2 text-gray-400">
                      {b.match_date ? format(new Date(b.match_date), "d MMM", { locale: fr }) : "—"}
                    </td>
                    <td className="py-1.5 px-2 truncate max-w-[180px]"
                        title={`${b.home_team} vs ${b.away_team}`}>
                      <span className={cn(b.outcome === "HOME" && "text-brand-300")}>{b.home_team}</span>
                      <span className="text-gray-600 mx-1">vs</span>
                      <span className={cn(b.outcome === "AWAY" && "text-brand-300")}>{b.away_team}</span>
                    </td>
                    <td className="py-1.5 px-2">
                      <span className={cn(
                        "px-1.5 py-0.5 rounded text-[10px] font-semibold",
                        b.market === "FOOTBALL_OU" ? "bg-purple-500/20 text-purple-300" :
                        b.market === "FOOTBALL_AH" ? "bg-teal-500/20 text-teal-300" :
                        b.market === "NBA" ? "bg-orange-500/20 text-orange-300" :
                        "bg-brand-500/20 text-brand-300"
                      )}>
                        {b.market === "FOOTBALL_OU" ? "O/U" :
                         b.market === "FOOTBALL_AH" ? "AH" :
                         b.market === "NBA" ? "NBA" : "1X2"}
                      </span>
                    </td>
                    <td className="py-1.5 px-2 truncate max-w-[140px]"
                        title={b.outcome_label}>{b.outcome_label}</td>
                    <td className="py-1.5 px-2 text-right font-mono"
                        title={b.opening_odds
                          ? `Opening: ${b.opening_odds.toFixed(2)} → Closing: ${b.odds.toFixed(2)}`
                          : `Cote actuelle: ${b.odds.toFixed(2)}`}>
                      {b.opening_odds && b.opening_odds !== b.odds && (
                        <span className="text-[9px] text-gray-600 mr-1">{b.opening_odds.toFixed(2)}→</span>
                      )}
                      {b.odds.toFixed(2)}
                    </td>
                    <td className="py-1.5 px-2 text-right">{b.edge_percent.toFixed(0)}%</td>
                    <td className={cn(
                      "py-1.5 px-2 text-right font-mono",
                      b.clv_percent === null ? "text-gray-600" :
                      b.clv_percent > 0 ? "text-green-400" :
                      b.clv_percent < 0 ? "text-red-400" : "text-gray-500"
                    )}>
                      {b.clv_percent === null ? "—" : `${b.clv_percent > 0 ? "+" : ""}${b.clv_percent.toFixed(1)}%`}
                    </td>
                    <td className="py-1.5 px-2 text-right">{b.stake.toFixed(0)}€</td>
                    <td className="py-1.5 px-2 text-gray-400">
                      {b.home_score !== null ? `${b.home_score}-${b.away_score}` : "—"}
                    </td>
                    <td className="py-1.5 px-2">
                      <StatusBadge bet={b} />
                    </td>
                    <td className={cn(
                      "py-1.5 px-2 text-right font-semibold",
                      b.profit !== null
                        ? (b.profit >= 0 ? "text-green-400" : "text-red-400")
                        : "text-gray-500"
                    )}>
                      {b.profit !== null ? `${b.profit >= 0 ? "+" : ""}${b.profit.toFixed(0)}€` : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {bets.length > 100 && (
              <p className="text-xs text-gray-500 mt-3 text-center">
                Affichage des 100 derniers paris sur {bets.length}.
              </p>
            )}
          </div>
        )}
      </div>

      {/* Note méthodo */}
      <div className="bg-gray-900 border border-gray-800 rounded-xl p-4 text-xs text-gray-500">
        <p className="font-semibold text-gray-300 mb-1">Méthode</p>
        <p>
          Pour chaque match avec prédiction, on simule un pari Kelly fractionné
          ({(0.25 * 100).toFixed(0)}%) sur une bankroll de référence (100€), si l'edge est
          dans [{(0.08 * 100).toFixed(0)}%, {(0.20 * 100).toFixed(0)}%] et la ligue est whitelistée.
          Le P&L est calculé sur les matchs FINISHED uniquement. Différence avec le backtest :
          ici on observe les prédictions <strong>réelles</strong> du modèle en prod, pas du OOF simulé.
        </p>
      </div>
    </div>
  );
}
