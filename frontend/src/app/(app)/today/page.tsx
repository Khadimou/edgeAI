"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { format } from "date-fns";
import { fr } from "date-fns/locale";
import {
  Flame, Clock, TrendingUp, ChevronRight, AlertCircle,
  CheckCircle2, Calendar, Zap,
} from "lucide-react";
import { api } from "@/lib/api";
import { formatCurrency, formatPercent, cn } from "@/lib/utils";

interface ValueBet {
  outcome: string;
  outcome_label: string;
  market?: "1X2" | "OU_2_5" | "AH";
  odds: number;
  edge: number;
  edge_percent: number;
  recommended_amount: number;
  potential_gain: number;
  prob: number;
  strategy: string;
}

interface Tier {
  label: string;
  color: string;
  fire: number;
}

interface TodayPick {
  match_id: string;
  sport?: string;
  home_team: string;
  away_team: string;
  league: string;
  match_date: string;
  kickoff_minutes: number;
  prob_home: number | null;
  prob_draw: number | null;
  prob_away: number | null;
  prob_over_25?: number | null;
  prob_under_25?: number | null;
  confidence: number | null;
  home_odds: number | null;
  draw_odds: number | null;
  away_odds: number | null;
  over_25_odds?: number | null;
  under_25_odds?: number | null;
  best_bet: ValueBet | null;
  all_value_bets: ValueBet[];
  tier: Tier | null;
  has_value: boolean;
  league_whitelisted?: boolean;
  ou_whitelisted?: boolean;
}

interface TodayData {
  date: string;
  total_matches: number;
  value_matches: number;
  total_recommended: number;
  bankroll: number;
  picks: TodayPick[];
}

function FireBadge({ count }: { count: number }) {
  if (count === 0) return null;
  return (
    <span className="text-base leading-none">
      {"🔥".repeat(count)}
    </span>
  );
}

function KickoffBadge({ minutes }: { minutes: number }) {
  if (minutes <= 0) return (
    <span className="text-xs bg-green-500/20 text-green-400 px-2 py-0.5 rounded-full font-semibold animate-pulse">
      Live
    </span>
  );
  if (minutes < 60) return (
    <span className="text-xs bg-yellow-500/20 text-yellow-400 px-2 py-0.5 rounded-full font-semibold">
      Dans {minutes}min
    </span>
  );
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  return (
    <span className="text-xs bg-gray-700 text-gray-400 px-2 py-0.5 rounded-full">
      Dans {h}h{m > 0 ? `${m.toString().padStart(2, "0")}` : ""}
    </span>
  );
}

function TierBadge({ tier }: { tier: Tier }) {
  const colorMap: Record<string, string> = {
    green: "bg-green-500/20 text-green-400 border-green-500/30",
    yellow: "bg-yellow-500/20 text-yellow-400 border-yellow-500/30",
    blue: "bg-brand-500/20 text-brand-400 border-brand-500/30",
    gray: "bg-gray-700 text-gray-400 border-gray-600",
  };
  return (
    <span className={cn("text-xs px-2 py-0.5 rounded-full border font-semibold", colorMap[tier.color] ?? colorMap.gray)}>
      {tier.label}
    </span>
  );
}

function PickCard({ pick }: { pick: TodayPick }) {
  const bet = pick.best_bet;
  const tier = pick.tier;

  const isTop = tier && tier.fire >= 2;

  return (
    <Link
      href={`/match/${pick.match_id}`}
      className={cn(
        "block card hover:border-gray-600 transition-all group",
        isTop && "border-brand-500/40 bg-brand-500/5 hover:border-brand-500/60",
      )}
    >
      {/* Header */}
      <div className="flex items-start justify-between gap-3 mb-3">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap mb-1">
            {pick.sport === "NBA" && (
              <span className="text-[10px] font-bold px-1.5 py-0.5 rounded bg-orange-600 text-white">
                NBA
              </span>
            )}
            {tier && <TierBadge tier={tier} />}
            {tier && <FireBadge count={tier.fire} />}
            <KickoffBadge minutes={pick.kickoff_minutes} />
          </div>
          <p className="font-semibold text-white truncate">
            {pick.home_team} <span className="text-gray-500 font-normal">vs</span> {pick.away_team}
          </p>
          <p className="text-xs text-gray-500 flex items-center gap-1 mt-0.5">
            <Clock className="w-3 h-3" />
            {pick.league} · {format(new Date(pick.match_date), "HH:mm", { locale: fr })}
          </p>
        </div>
        <ChevronRight className="w-4 h-4 text-gray-600 group-hover:text-gray-400 shrink-0 mt-1" />
      </div>

      {/* Value bet highlight */}
      {bet ? (
        <div className={cn(
          "rounded-lg p-3 flex items-center justify-between gap-3",
          isTop ? "bg-brand-600/15 border border-brand-500/20" : "bg-gray-800/60"
        )}>
          <div>
            <div className="flex items-center gap-2 mb-0.5 flex-wrap">
              <p className="text-xs text-gray-400">Mise conseillée — {bet.outcome_label}</p>
              {bet.market === "OU_2_5" && (
                <span className="text-[10px] px-1.5 py-0.5 rounded bg-purple-500/20 text-purple-300 font-semibold">
                  Buts
                </span>
              )}
              {bet.market === "AH" && (
                <span className="text-[10px] px-1.5 py-0.5 rounded bg-teal-500/20 text-teal-300 font-semibold">
                  Handicap
                </span>
              )}
            </div>
            <div className="flex items-center gap-2">
              <span className={cn(
                "text-xs px-1.5 py-0.5 rounded font-mono font-bold",
                bet.edge_percent >= 15 ? "bg-green-500/20 text-green-400" :
                bet.edge_percent >= 10 ? "bg-yellow-500/20 text-yellow-400" :
                "bg-gray-700 text-gray-300"
              )}>
                Edge +{bet.edge_percent.toFixed(0)}%
              </span>
              <span className="text-sm text-gray-400">Cote {bet.odds.toFixed(2)}</span>
            </div>
          </div>
          <div className="text-right shrink-0">
            <p className="text-lg font-bold text-brand-400">{formatCurrency(bet.recommended_amount)}</p>
            <p className="text-xs text-green-400 flex items-center gap-0.5 justify-end">
              <TrendingUp className="w-3 h-3" />
              +{formatCurrency(bet.potential_gain)}
            </p>
          </div>
        </div>
      ) : (
        <div className="rounded-lg p-3 bg-gray-800/40 text-center">
          <p className="text-xs text-gray-500">Aucun value bet détecté — match à surveiller</p>
        </div>
      )}

      {/* Probas */}
      {pick.prob_home !== null && (
        <div className="mt-3 pt-3 border-t border-gray-800/60 flex gap-4 text-xs text-gray-500">
          <span className={cn(bet?.outcome === "HOME" && "text-brand-400 font-semibold")}>
            Dom. {formatPercent(pick.prob_home!)}
          </span>
          {pick.sport !== "NBA" && (
            <span className={cn(bet?.outcome === "DRAW" && "text-brand-400 font-semibold")}>
              Nul {formatPercent(pick.prob_draw!)}
            </span>
          )}
          <span className={cn(bet?.outcome === "AWAY" && "text-brand-400 font-semibold")}>
            Ext. {formatPercent(pick.prob_away!)}
          </span>
          {bet && (
            <span className="ml-auto text-gray-600">
              Confiance {formatPercent(pick.confidence!)}
            </span>
          )}
        </div>
      )}
    </Link>
  );
}

export default function TodayPage() {
  const [sport, setSport] = useState<"FOOTBALL" | "NBA">("FOOTBALL");
  const { data, isLoading, isError } = useQuery<TodayData>({
    queryKey: ["today", sport],
    queryFn: () => api.get("/today", { params: { sport } }).then((r) => r.data),
    staleTime: 5 * 60 * 1000,
    refetchInterval: 10 * 60 * 1000,
  });

  const today = format(new Date(), "EEEE d MMMM", { locale: fr });
  const todayCap = today.charAt(0).toUpperCase() + today.slice(1);

  if (isLoading) {
    return (
      <div className="space-y-4 max-w-2xl">
        <div className="h-8 w-64 bg-gray-800 rounded animate-pulse" />
        {[1, 2, 3].map((i) => (
          <div key={i} className="card animate-pulse h-36 bg-gray-800/50" />
        ))}
      </div>
    );
  }

  if (isError) {
    return (
      <div className="card border-red-500/30 bg-red-500/5 text-center py-10 max-w-2xl">
        <AlertCircle className="w-8 h-8 text-red-400 mx-auto mb-2" />
        <p className="text-red-400 font-medium">Erreur de chargement</p>
      </div>
    );
  }

  const topPicks = data?.picks.filter((p) => p.has_value) ?? [];
  const watchOnly = data?.picks.filter((p) => !p.has_value) ?? [];
  const filteredOutByLeague = data?.picks.filter(
    (p) => p.league_whitelisted === false
  ) ?? [];

  return (
    <div className="space-y-8 max-w-2xl">
      {/* Header */}
      <div>
        <div className="flex items-center justify-between gap-3 mb-1 flex-wrap">
          <div className="flex items-center gap-2">
            <Calendar className="w-5 h-5 text-brand-400" />
            <h1 className="text-2xl font-bold">{todayCap}</h1>
          </div>
          {/* Sport switcher */}
          <div className="flex gap-1 bg-gray-900 border border-gray-800 rounded-lg p-1">
            <button
              onClick={() => setSport("FOOTBALL")}
              className={cn(
                "px-3 py-1.5 rounded-md text-xs font-semibold transition-colors",
                sport === "FOOTBALL"
                  ? "bg-brand-600 text-white"
                  : "text-gray-400 hover:text-gray-100"
              )}
            >
              ⚽ Football
            </button>
            <button
              onClick={() => setSport("NBA")}
              className={cn(
                "px-3 py-1.5 rounded-md text-xs font-semibold transition-colors",
                sport === "NBA"
                  ? "bg-orange-600 text-white"
                  : "text-gray-400 hover:text-gray-100"
              )}
            >
              🏀 NBA
            </button>
          </div>
        </div>
        <p className="text-gray-400 text-sm">
          {sport === "NBA"
            ? "Matchs NBA — mise à jour 1×/jour (API the-odds-api)"
            : "Matchs du jour à ne pas manquer — mise à jour toutes les 10 min"}
        </p>
      </div>

      {/* Résumé du jour */}
      {data && (
        <div className="grid grid-cols-3 gap-2 sm:gap-3">
          <div className="stat-card">
            <div className="stat-label">Matchs aujourd'hui</div>
            <div className="stat-value">{data.total_matches}</div>
          </div>
          <div className="stat-card">
            <div className="stat-label">Value bets</div>
            <div className="stat-value text-brand-400">{data.value_matches}</div>
          </div>
          <div className="stat-card">
            <div className="stat-label">À miser au total</div>
            <div className="stat-value text-edge-green">
              {data.total_recommended > 0 ? formatCurrency(data.total_recommended) : "—"}
            </div>
          </div>
        </div>
      )}

      {/* Banner : filtre La Liga actif */}
      {filteredOutByLeague.length > 0 && (
        <div className="rounded-xl border border-yellow-500/30 bg-yellow-500/10 p-4 flex items-start gap-3">
          <AlertCircle className="w-5 h-5 text-yellow-400 shrink-0 mt-0.5" />
          <div className="text-sm">
            <p className="font-semibold text-yellow-300">
              Value bets limités à La Liga
            </p>
            <p className="text-gray-300 mt-1">
              Le backtest historique a montré que seule La Liga est rentable (ROI +14%).
              Les {filteredOutByLeague.length} match{filteredOutByLeague.length > 1 ? "s" : ""} d'autres
              ligues du jour s'affichent ci-dessous mais sans recommandation Kelly.
              {" "}
              <Link href="/backtest" className="text-yellow-400 hover:underline">
                Voir les détails du backtest →
              </Link>
            </p>
          </div>
        </div>
      )}

      {/* Value bets du jour */}
      {topPicks.length > 0 ? (
        <section>
          <div className="flex items-center gap-2 mb-4">
            <Flame className="w-5 h-5 text-orange-400" />
            <h2 className="text-lg font-semibold">
              {topPicks.length} match{topPicks.length > 1 ? "s" : ""} à jouer
            </h2>
            <span className="text-xs text-gray-500">· meilleure edge en premier</span>
          </div>
          <div className="space-y-3">
            {topPicks.map((pick) => (
              <PickCard key={pick.match_id} pick={pick} />
            ))}
          </div>
        </section>
      ) : (
        <div className="card border-dashed border-gray-700 text-center py-12">
          <Zap className="w-10 h-10 text-gray-600 mx-auto mb-3" />
          <p className="font-medium mb-1">Aucun value bet aujourd'hui</p>
          <p className="text-sm text-gray-400">
            {data?.total_matches === 0
              ? "Pas de matchs schedulés aujourd'hui."
              : "Le modèle ne détecte pas d'avantage suffisant sur les matchs du jour."}
          </p>
          <Link href="/plan" className="mt-4 inline-block text-sm text-brand-400 hover:underline">
            Voir le plan 72h →
          </Link>
        </div>
      )}

      {/* Autres matchs du jour (sans value bet) */}
      {watchOnly.length > 0 && (
        <section>
          <div className="flex items-center gap-2 mb-3">
            <CheckCircle2 className="w-4 h-4 text-gray-500" />
            <h2 className="text-base font-semibold text-gray-400">
              Autres matchs du jour ({watchOnly.length})
            </h2>
          </div>
          <div className="space-y-2">
            {watchOnly.map((pick) => (
              <Link
                key={pick.match_id}
                href={`/match/${pick.match_id}`}
                className="card flex items-center justify-between hover:border-gray-700 transition-colors group py-3"
              >
                <div>
                  <p className="font-medium text-sm">
                    {pick.home_team} <span className="text-gray-500">vs</span> {pick.away_team}
                  </p>
                  <p className="text-xs text-gray-500 mt-0.5">
                    {pick.league} · {format(new Date(pick.match_date), "HH:mm", { locale: fr })}
                  </p>
                </div>
                <div className="flex items-center gap-3">
                  {pick.prob_home !== null && (
                    <span className="text-xs text-gray-500">
                      {formatPercent(pick.prob_home)} · {formatPercent(pick.prob_draw!)} · {formatPercent(pick.prob_away!)}
                    </span>
                  )}
                  <ChevronRight className="w-4 h-4 text-gray-600 group-hover:text-gray-400" />
                </div>
              </Link>
            ))}
          </div>
        </section>
      )}

      {/* Note stratégique */}
      {topPicks.length > 0 && (
        <div className="card border-gray-800 bg-gray-900/50">
          <div className="flex gap-3">
            <Zap className="w-4 h-4 text-yellow-400 shrink-0 mt-0.5" />
            <p className="text-xs text-gray-400">
              Les mises sont calculées via Kelly fractionné. Ne jamais dépasser le montant conseillé.
              Le total recommandé ({formatCurrency(data?.total_recommended ?? 0)}) représente{" "}
              {data?.bankroll
                ? `${((data.total_recommended / data.bankroll) * 100).toFixed(1)}% de votre bankroll.`
                : "un pourcentage de votre bankroll."}
            </p>
          </div>
        </div>
      )}
    </div>
  );
}
