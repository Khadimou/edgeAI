"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip,
  ResponsiveContainer, ReferenceLine,
} from "recharts";
import {
  TrendingUp, TrendingDown, AlertTriangle, CheckCircle2,
  FlaskConical, History, AlertCircle,
} from "lucide-react";
import { backtestApi } from "@/lib/api";
import { cn } from "@/lib/utils";

interface Summary {
  initial_bankroll: number;
  final_bankroll: number;
  n_bets: number;
  n_wins: number;
  hit_rate: number;
  total_staked: number;
  total_pnl: number;
  roi_percent: number;
  yield_per_bet: number;
  max_drawdown_pct: number;
  peak_bankroll: number;
  avg_odds: number;
  avg_edge_pct: number;
  profit_factor: number;
  period_start: string;
  period_end: string;
  per_league: Record<string, { n_bets: number; hit_rate: number; roi_percent: number; total_pnl: number }>;
  params: {
    edge_threshold: number;
    kelly_fraction: number;
    max_stake_fraction: number;
    only_best_per_match: boolean;
  };
}

interface EquityPoint { date: string; bankroll: number }

interface SampleBet {
  date: string; league: string; home_team: string; away_team: string;
  bet_on: string; odds: number; prob: number; edge: number; stake: number;
  actual: string; won: boolean; profit: number; bankroll: number;
}

interface BacktestData {
  summary: Summary;
  equity_curve: EquityPoint[];
  sample_bets: SampleBet[];
  computed_at: string;
}

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

function Verdict({ summary }: { summary: Summary }) {
  const profitable = summary.roi_percent > 0;
  const drawdownOk = summary.max_drawdown_pct < 30;
  const sampleOk = summary.n_bets >= 100;

  let level: "good" | "warn" | "bad";
  let title: string;
  let detail: string;

  if (profitable && drawdownOk && sampleOk) {
    level = "good";
    title = "Stratégie viable";
    detail = "ROI positif, drawdown maîtrisé. La stratégie passe le test historique.";
  } else if (summary.roi_percent < -5 || summary.max_drawdown_pct > 50) {
    level = "bad";
    title = "Stratégie non viable en l'état";
    detail = `Le suivi aveugle de ce modèle aurait fait perdre ${Math.abs(summary.roi_percent).toFixed(1)}% du capital, avec un drawdown de ${summary.max_drawdown_pct.toFixed(0)}%. Le modèle est trop confiant sur les outsiders (edge moyen ${summary.avg_edge_pct.toFixed(0)}% irréaliste).`;
  } else {
    level = "warn";
    title = "Résultat marginal";
    detail = `ROI ${summary.roi_percent.toFixed(1)}%, drawdown ${summary.max_drawdown_pct.toFixed(0)}%. La stratégie n'est pas clairement rentable — affiner le seuil d'edge ou recalibrer le modèle.`;
  }

  const config = {
    good: { Icon: CheckCircle2, color: "border-green-500/30 bg-green-500/10 text-green-400" },
    warn: { Icon: AlertTriangle, color: "border-yellow-500/30 bg-yellow-500/10 text-yellow-400" },
    bad: { Icon: AlertCircle, color: "border-red-500/30 bg-red-500/10 text-red-400" },
  }[level];

  const Icon = config.Icon;
  return (
    <div className={cn("rounded-xl border p-5", config.color)}>
      <div className="flex items-start gap-3">
        <Icon className="w-6 h-6 shrink-0 mt-0.5" />
        <div>
          <p className="font-bold">{title}</p>
          <p className="text-sm mt-1 text-gray-300">{detail}</p>
        </div>
      </div>
    </div>
  );
}

type Market = "FOOTBALL_1X2" | "FOOTBALL_OU" | "FOOTBALL_AH" | "NBA" | "NBA_TOTALS";
const MARKET_LABELS: Record<Market, { emoji: string; label: string; color: string }> = {
  FOOTBALL_1X2: { emoji: "⚽", label: "Foot 1X2", color: "bg-brand-600" },
  FOOTBALL_OU: { emoji: "⚽", label: "Foot O/U", color: "bg-purple-600" },
  FOOTBALL_AH: { emoji: "⚽", label: "Foot AH", color: "bg-teal-600" },
  NBA: { emoji: "🏀", label: "NBA ML", color: "bg-orange-600" },
  NBA_TOTALS: { emoji: "🏀", label: "NBA Totals", color: "bg-pink-600" },
};

export default function BacktestPage() {
  const [market, setMarket] = useState<Market>("FOOTBALL_1X2");
  // alias for older code in this file
  const sport = market === "NBA" ? "NBA" : "FOOTBALL";
  const setSport = (s: "FOOTBALL" | "NBA") =>
    setMarket(s === "NBA" ? "NBA" : "FOOTBALL_1X2");
  const { data, isLoading, error } = useQuery<BacktestData>({
    queryKey: ["backtest-latest", market],
    queryFn: () => backtestApi.latest(market).then((r) => r.data),
    retry: false,
  });

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin w-8 h-8 border-2 border-brand-500 border-t-transparent rounded-full" />
      </div>
    );
  }

  if (error || !data) {
    return (
      <div className="space-y-4">
        <div className="flex items-center justify-between gap-3 flex-wrap">
          <h1 className="text-2xl font-bold flex items-center gap-2">
            <FlaskConical className="w-6 h-6 text-brand-500" />
            Backtest historique
          </h1>
          <div className="flex gap-1 bg-gray-900 border border-gray-800 rounded-lg p-1 flex-wrap">
            {(Object.keys(MARKET_LABELS) as Market[]).map((m) => {
              const cfg = MARKET_LABELS[m];
              return (
                <button
                  key={m}
                  onClick={() => setMarket(m)}
                  className={cn(
                    "px-3 py-1.5 rounded-md text-xs font-semibold transition-colors",
                    market === m ? `${cfg.color} text-white` : "text-gray-400 hover:text-gray-100"
                  )}
                >
                  {cfg.emoji} {cfg.label}
                </button>
              );
            })}
          </div>
        </div>
        <div className="bg-gray-900 border border-gray-800 rounded-xl p-8 text-center">
          <FlaskConical className="w-10 h-10 text-gray-600 mx-auto mb-3" />
          <p className="text-gray-400 font-medium">Aucun backtest {MARKET_LABELS[market].label} disponible</p>
          <p className="text-xs text-gray-600 mt-2">
            Lancez le calcul dans le ml_worker :
          </p>
          <pre className="text-xs bg-gray-950 text-gray-300 p-3 rounded mt-2 inline-block">
            docker exec edgeai-ml_worker-1 python {market === "NBA" ? "nba_backtest.py" : market === "FOOTBALL_OU" ? "ou_backtest.py" : "backtest.py"}
          </pre>
        </div>
      </div>
    );
  }

  const { summary, equity_curve, sample_bets } = data;

  return (
    <div className="space-y-6">
      <div>
        <div className="flex items-center justify-between gap-3 flex-wrap">
          <h1 className="text-2xl font-bold flex items-center gap-2">
            <FlaskConical className="w-6 h-6 text-brand-500" />
            Backtest historique
          </h1>
          <div className="flex gap-1 bg-gray-900 border border-gray-800 rounded-lg p-1 flex-wrap">
            {(Object.keys(MARKET_LABELS) as Market[]).map((m) => {
              const cfg = MARKET_LABELS[m];
              return (
                <button
                  key={m}
                  onClick={() => setMarket(m)}
                  className={cn(
                    "px-3 py-1.5 rounded-md text-xs font-semibold transition-colors",
                    market === m ? `${cfg.color} text-white` : "text-gray-400 hover:text-gray-100"
                  )}
                >
                  {cfg.emoji} {cfg.label}
                </button>
              );
            })}
          </div>
        </div>
        <p className="text-sm text-gray-400 mt-1">
          Simulation {MARKET_LABELS[market].label} : si vous aviez suivi les value bets du modèle entre {summary.period_start} et {summary.period_end}.
          Cotes réelles {market === "NBA" ? "moneyline US bookmakers" : "Pinnacle/Bet365"}.
        </p>
      </div>

      <Verdict summary={summary} />

      {/* KPIs principaux */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <Kpi
          label="ROI"
          value={`${summary.roi_percent > 0 ? "+" : ""}${summary.roi_percent.toFixed(1)}%`}
          sub={`P&L: ${summary.total_pnl > 0 ? "+" : ""}${summary.total_pnl.toFixed(0)}€`}
          accent={summary.roi_percent > 0 ? "good" : "bad"}
        />
        <Kpi
          label="Bankroll final"
          value={`${summary.final_bankroll.toFixed(0)}€`}
          sub={`départ: ${summary.initial_bankroll.toFixed(0)}€ · pic: ${summary.peak_bankroll.toFixed(0)}€`}
          accent={summary.final_bankroll >= summary.initial_bankroll ? "good" : "bad"}
        />
        <Kpi
          label="Hit rate"
          value={`${(summary.hit_rate * 100).toFixed(1)}%`}
          sub={`${summary.n_wins}/${summary.n_bets} paris gagnés`}
        />
        <Kpi
          label="Max drawdown"
          value={`${summary.max_drawdown_pct.toFixed(1)}%`}
          sub={summary.max_drawdown_pct > 30 ? "risque élevé" : "maîtrisé"}
          accent={summary.max_drawdown_pct > 30 ? "bad" : summary.max_drawdown_pct > 15 ? "warn" : "good"}
        />
      </div>

      {/* KPIs secondaires */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <Kpi
          label="Profit factor"
          value={summary.profit_factor.toFixed(2)}
          sub="gains / pertes"
          accent={summary.profit_factor > 1 ? "good" : "bad"}
        />
        <Kpi
          label="Cote moyenne"
          value={summary.avg_odds.toFixed(2)}
          sub={summary.avg_odds > 3 ? "surtout outsiders" : "favoris/intermédiaires"}
          accent={summary.avg_odds > 4 ? "warn" : "default"}
        />
        <Kpi
          label="Edge moyen"
          value={`${summary.avg_edge_pct.toFixed(1)}%`}
          sub={summary.avg_edge_pct > 20 ? "trop élevé = calibration faible" : "réaliste"}
          accent={summary.avg_edge_pct > 30 ? "warn" : "default"}
        />
        <Kpi
          label="Yield / pari"
          value={`${summary.yield_per_bet > 0 ? "+" : ""}${summary.yield_per_bet.toFixed(2)}€`}
          sub="par mise placée"
          accent={summary.yield_per_bet > 0 ? "good" : "bad"}
        />
      </div>

      {/* Equity curve */}
      {equity_curve.length > 1 && (
        <div className="bg-gray-900 border border-gray-800 rounded-xl p-5">
          <h2 className="text-sm font-semibold mb-4 text-gray-300 flex items-center gap-2">
            <History className="w-4 h-4" />
            Courbe de bankroll
          </h2>
          <div className="h-72">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={equity_curve}>
                <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
                <XAxis
                  dataKey="date"
                  stroke="#6b7280"
                  tick={{ fontSize: 11 }}
                  tickFormatter={(d) =>
                    new Date(d).toLocaleDateString("fr-FR", { day: "2-digit", month: "2-digit" })
                  }
                />
                <YAxis
                  stroke="#6b7280"
                  tick={{ fontSize: 11 }}
                  tickFormatter={(v) => `${v}€`}
                />
                <Tooltip
                  contentStyle={{ backgroundColor: "#111827", border: "1px solid #1f2937", borderRadius: 8 }}
                  labelFormatter={(d) => new Date(d).toLocaleDateString("fr-FR")}
                  formatter={(val: number) => [`${val.toFixed(2)}€`, "Bankroll"]}
                />
                <ReferenceLine
                  y={summary.initial_bankroll}
                  stroke="#6b7280"
                  strokeDasharray="3 3"
                  label={{ value: "Départ", fill: "#6b7280", fontSize: 10, position: "insideTopRight" }}
                />
                <Line
                  type="monotone"
                  dataKey="bankroll"
                  stroke={summary.roi_percent >= 0 ? "#22d3ee" : "#f87171"}
                  strokeWidth={2}
                  dot={false}
                />
              </LineChart>
            </ResponsiveContainer>
          </div>
        </div>
      )}

      {/* Par ligue */}
      <div className="bg-gray-900 border border-gray-800 rounded-xl p-5">
        <h2 className="text-sm font-semibold mb-4 text-gray-300">Performance par ligue</h2>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-xs text-gray-500 border-b border-gray-800">
                <th className="py-2 pr-3">Ligue</th>
                <th className="py-2 px-3 text-right">Paris</th>
                <th className="py-2 px-3 text-right">Hit rate</th>
                <th className="py-2 px-3 text-right">ROI</th>
                <th className="py-2 px-3 text-right">P&L</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(summary.per_league)
                .sort(([, a], [, b]) => b.roi_percent - a.roi_percent)
                .map(([league, stats]) => (
                  <tr key={league} className="border-b border-gray-800/50">
                    <td className="py-2 pr-3 font-medium">{league}</td>
                    <td className="py-2 px-3 text-right text-gray-400">{stats.n_bets}</td>
                    <td className="py-2 px-3 text-right">{(stats.hit_rate * 100).toFixed(1)}%</td>
                    <td className={cn(
                      "py-2 px-3 text-right font-semibold",
                      stats.roi_percent >= 0 ? "text-green-400" : "text-red-400"
                    )}>
                      {stats.roi_percent >= 0 ? "+" : ""}{stats.roi_percent.toFixed(1)}%
                    </td>
                    <td className={cn(
                      "py-2 px-3 text-right",
                      stats.total_pnl >= 0 ? "text-green-400" : "text-red-400"
                    )}>
                      {stats.total_pnl >= 0 ? "+" : ""}{stats.total_pnl.toFixed(0)}€
                    </td>
                  </tr>
                ))}
            </tbody>
          </table>
        </div>
      </div>

      {/* Échantillon de paris */}
      {sample_bets.length > 0 && (
        <div className="bg-gray-900 border border-gray-800 rounded-xl p-5">
          <h2 className="text-sm font-semibold mb-4 text-gray-300">
            Premiers paris simulés (top 50)
          </h2>
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-left text-gray-500 border-b border-gray-800">
                  <th className="py-2 pr-2">Date</th>
                  <th className="py-2 px-2">Match</th>
                  <th className="py-2 px-2">Pari</th>
                  <th className="py-2 px-2 text-right">Cote</th>
                  <th className="py-2 px-2 text-right">Edge</th>
                  <th className="py-2 px-2 text-right">Mise</th>
                  <th className="py-2 px-2">Réel</th>
                  <th className="py-2 px-2 text-right">P&L</th>
                </tr>
              </thead>
              <tbody>
                {sample_bets.map((b, i) => (
                  <tr key={i} className="border-b border-gray-800/40">
                    <td className="py-1.5 pr-2 text-gray-400">{b.date}</td>
                    <td className="py-1.5 px-2 truncate max-w-[200px]">
                      {b.home_team} vs {b.away_team}
                    </td>
                    <td className="py-1.5 px-2">{b.bet_on}</td>
                    <td className="py-1.5 px-2 text-right">{b.odds.toFixed(2)}</td>
                    <td className="py-1.5 px-2 text-right">{(b.edge * 100).toFixed(0)}%</td>
                    <td className="py-1.5 px-2 text-right">{b.stake.toFixed(0)}€</td>
                    <td className="py-1.5 px-2">{b.actual}</td>
                    <td className={cn(
                      "py-1.5 px-2 text-right font-semibold",
                      b.profit >= 0 ? "text-green-400" : "text-red-400"
                    )}>
                      {b.profit >= 0 ? "+" : ""}{b.profit.toFixed(0)}€
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Paramètres */}
      <div className="bg-gray-900 border border-gray-800 rounded-xl p-5 text-xs text-gray-400">
        <p className="font-semibold text-gray-300 mb-2">Paramètres de simulation</p>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <div>Seuil edge : {(summary.params.edge_threshold * 100).toFixed(0)}%</div>
          <div>Kelly fraction : {summary.params.kelly_fraction}</div>
          <div>Mise max / bankroll : {(summary.params.max_stake_fraction * 100).toFixed(0)}%</div>
          <div>1 pari / match : {summary.params.only_best_per_match ? "oui" : "non"}</div>
        </div>
        <p className="mt-3 text-[10px] text-gray-600">
          Méthode : OOF predictions (5-fold TimeSeriesSplit, sans data leakage), Kelly fractionné,
          cotes Pinnacle/Bet365 à la clôture. Calculé {new Date(data.computed_at).toLocaleString("fr-FR")}.
        </p>
      </div>
    </div>
  );
}
