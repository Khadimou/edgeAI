"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { format } from "date-fns";
import { fr } from "date-fns/locale";
import {
  Target, TrendingUp, Clock, AlertCircle, ChevronRight,
  Zap, Trophy, ArrowRight,
} from "lucide-react";
import { api } from "@/lib/api";
import { useAuthStore } from "@/store/auth";
import { formatCurrency, formatPercent, outcomeLabel, cn } from "@/lib/utils";

interface PlanBet {
  match_id: string;
  home_team: string;
  away_team: string;
  league: string;
  match_date: string;
  outcome: string;
  outcome_label: string;
  odds: number;
  edge: number;
  edge_percent: number;
  recommended_amount: number;
  potential_gain: number;
  prob_home: number;
  prob_draw: number;
  prob_away: number;
  confidence: number;
  strategy: string;
}

interface GoalSummary {
  goal_amount: number;
  goal_timeframe_days: number;
  days_elapsed: number;
  days_remaining: number;
  target_bankroll: number;
  current_profit: number;
  progress_percent: number;
  required_roi_percent: number;
  weekly_roi_needed: number;
  on_track: boolean;
}

interface PlanData {
  has_goal: boolean;
  bankroll: number;
  goal_summary: GoalSummary | null;
  bets: PlanBet[];
  message: string;
}

function EdgeBadge({ edge }: { edge: number }) {
  const pct = (edge * 100).toFixed(1);
  const color = edge >= 0.08 ? "bg-green-500/20 text-green-400 border-green-500/30"
    : edge >= 0.04 ? "bg-yellow-500/20 text-yellow-400 border-yellow-500/30"
    : "bg-gray-700 text-gray-400 border-gray-600";
  return (
    <span className={cn("text-xs px-2 py-0.5 rounded-full border font-semibold", color)}>
      Edge +{pct}%
    </span>
  );
}

export default function PlanPage() {
  const { user } = useAuthStore();

  const { data, isLoading, isError, error } = useQuery<PlanData>({
    queryKey: ["plan"],
    queryFn: () => api.get("/plan").then((r) => r.data),
    staleTime: 5 * 60 * 1000,
    retry: 1,
  });

  if (isLoading) {
    return (
      <div className="space-y-4">
        {[1, 2, 3].map((i) => (
          <div key={i} className="card animate-pulse h-24 bg-gray-800/50" />
        ))}
      </div>
    );
  }

  if (isError) {
    return (
      <div className="card border-red-500/30 bg-red-500/5 text-center py-10">
        <AlertCircle className="w-8 h-8 text-red-400 mx-auto mb-2" />
        <p className="font-medium text-red-400 mb-1">Erreur lors du chargement du plan</p>
        <p className="text-sm text-gray-400">
          {(error as { response?: { status: number } })?.response?.status === 401
            ? "Session expirée — veuillez vous reconnecter."
            : "Une erreur serveur s'est produite. Réessayez dans quelques instants."}
        </p>
      </div>
    );
  }

  const gs = data?.goal_summary;

  return (
    <div className="space-y-8 max-w-3xl">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">Mon Plan de Mise</h1>
          <p className="text-gray-400 mt-1 text-sm">
            Mises calculées par le critère de Kelly selon votre profil et votre objectif
          </p>
        </div>
        <Link href="/settings" className="btn-secondary text-sm flex items-center gap-1.5">
          <Target className="w-4 h-4" />
          {data?.has_goal ? "Modifier l'objectif" : "Définir un objectif"}
        </Link>
      </div>

      {/* Bloc objectif */}
      {gs ? (
        <div className="card border-brand-500/30 bg-brand-500/5">
          <div className="flex items-start justify-between gap-4 mb-4">
            <div>
              <div className="flex items-center gap-2 mb-1">
                <Trophy className="w-4 h-4 text-brand-400" />
                <span className="font-semibold text-brand-400">
                  Objectif : +{formatCurrency(gs.goal_amount)} en {gs.goal_timeframe_days} jours
                </span>
                {gs.on_track ? (
                  <span className="text-xs bg-green-500/20 text-green-400 px-2 py-0.5 rounded-full">En bonne voie</span>
                ) : (
                  <span className="text-xs bg-red-500/20 text-red-400 px-2 py-0.5 rounded-full">En retard</span>
                )}
              </div>
              <p className="text-sm text-gray-400">
                {gs.days_elapsed === 0
                  ? "Objectif démarré aujourd'hui"
                  : `${gs.days_elapsed} jour${gs.days_elapsed > 1 ? "s" : ""} écoulés · ${gs.days_remaining} restants`}
              </p>
            </div>
            <div className="text-right shrink-0">
              <p className="text-2xl font-bold text-brand-400">
                {formatCurrency(gs.current_profit)}
              </p>
              <p className="text-xs text-gray-400">/ {formatCurrency(gs.goal_amount)}</p>
            </div>
          </div>

          {/* Barre de progression */}
          <div className="w-full bg-gray-800 rounded-full h-2 mb-4">
            <div
              className="bg-brand-500 h-2 rounded-full transition-all"
              style={{ width: `${gs.progress_percent}%` }}
            />
          </div>

          {/* Métriques */}
          <div className="grid grid-cols-3 gap-4 text-center">
            <div>
              <p className="text-xs text-gray-500">Progression</p>
              <p className="font-semibold text-sm">{gs.progress_percent.toFixed(1)}%</p>
            </div>
            <div>
              <p className="text-xs text-gray-500">ROI nécessaire</p>
              <p className="font-semibold text-sm">+{gs.required_roi_percent}%</p>
            </div>
            <div>
              <p className="text-xs text-gray-500">ROI/semaine cible</p>
              <p className="font-semibold text-sm">+{gs.weekly_roi_needed}%</p>
            </div>
          </div>
        </div>
      ) : (
        <div className="card border-dashed border-gray-700 text-center py-8">
          <Target className="w-10 h-10 text-gray-600 mx-auto mb-3" />
          <p className="font-medium mb-1">Aucun objectif défini</p>
          <p className="text-sm text-gray-400 mb-4">
            Définissez un gain cible et un horizon pour obtenir un plan de mise personnalisé.
          </p>
          <Link href="/settings" className="btn-primary inline-flex items-center gap-2">
            Définir mon objectif <ArrowRight className="w-4 h-4" />
          </Link>
        </div>
      )}

      {/* Paris conseillés */}
      <section>
        <h2 className="text-lg font-semibold mb-1">
          {data?.bets.length ? `${data.bets.length} mise${data.bets.length > 1 ? "s" : ""} conseillée${data.bets.length > 1 ? "s" : ""}` : "Aucune mise conseillée"}
          <span className="text-sm font-normal text-gray-400 ml-2">· 72h à venir</span>
        </h2>
        {data?.message && (
          <p className="text-sm text-gray-400 mb-4">{data.message}</p>
        )}

        {!data?.bets.length && (
          <div className="card text-center py-10">
            <AlertCircle className="w-8 h-8 text-gray-600 mx-auto mb-2" />
            <p className="text-gray-400 text-sm">
              {!data?.bankroll
                ? "Ajoutez votre bankroll dans les paramètres."
                : "Aucune opportunité sur les 72 prochaines heures. Revenez dans quelques heures."}
            </p>
          </div>
        )}

        <div className="space-y-3">
          {data?.bets.map((bet, i) => (
            <BetCard key={bet.match_id} bet={bet} rank={i + 1} />
          ))}
        </div>
      </section>

      {/* Rappel stratégie */}
      {(data?.bets.length ?? 0) > 0 && (
        <div className="card border-gray-800 bg-gray-900/50">
          <div className="flex gap-3">
            <Zap className="w-4 h-4 text-yellow-400 shrink-0 mt-0.5" />
            <p className="text-xs text-gray-400">
              Les mises sont calculées via le critère de Kelly fractionné selon votre profil{" "}
              <span className="text-gray-300 capitalize">{user?.risk_profile?.toLowerCase()}</span>.
              Ne jamais miser plus que le montant conseillé — c&apos;est conçu pour maximiser la croissance à long terme tout en limitant le risque de ruine.
            </p>
          </div>
        </div>
      )}
    </div>
  );
}

function BetCard({ bet, rank }: { bet: PlanBet; rank: number }) {
  return (
    <Link
      href={`/match/${bet.match_id}`}
      className="card hover:border-gray-700 transition-colors group block"
    >
      <div className="flex items-start justify-between gap-4">
        {/* Rang + infos match */}
        <div className="flex items-start gap-3 flex-1 min-w-0">
          <span className="w-6 h-6 rounded-full bg-gray-800 flex items-center justify-center text-xs font-bold text-gray-400 shrink-0 mt-0.5">
            {rank}
          </span>
          <div className="min-w-0">
            <div className="flex items-center gap-2 flex-wrap mb-1">
              <EdgeBadge edge={bet.edge} />
              <span className="text-sm font-semibold text-white">{bet.outcome_label}</span>
            </div>
            <p className="font-medium truncate">
              {bet.home_team} <span className="text-gray-500">vs</span> {bet.away_team}
            </p>
            <p className="text-xs text-gray-500 mt-0.5 flex items-center gap-1">
              <Clock className="w-3 h-3" />
              {bet.league} · {format(new Date(bet.match_date), "EEE d MMM HH:mm", { locale: fr })}
            </p>
            <p className="text-xs text-gray-600 mt-1 line-clamp-1">{bet.strategy}</p>
          </div>
        </div>

        {/* Mise + cote + gain */}
        <div className="text-right shrink-0">
          <p className="text-xs text-gray-500">Cote</p>
          <p className="text-lg font-bold">{bet.odds.toFixed(2)}</p>
          <div className="mt-1 pt-1 border-t border-gray-800">
            <p className="text-xs text-gray-500">Mise conseillée</p>
            <p className="font-bold text-brand-400">{formatCurrency(bet.recommended_amount)}</p>
            <p className="text-xs text-green-400">
              <TrendingUp className="w-3 h-3 inline mr-0.5" />
              +{formatCurrency(bet.potential_gain)}
            </p>
          </div>
        </div>

        <ChevronRight className="w-4 h-4 text-gray-600 group-hover:text-gray-400 transition-colors self-center" />
      </div>

      {/* Probas */}
      <div className="mt-3 pt-3 border-t border-gray-800 flex gap-4 text-xs text-gray-500">
        <span className={cn(bet.outcome === "HOME" && "text-brand-400 font-semibold")}>
          Dom. {formatPercent(bet.prob_home)}
        </span>
        <span className={cn(bet.outcome === "DRAW" && "text-brand-400 font-semibold")}>
          Nul {formatPercent(bet.prob_draw)}
        </span>
        <span className={cn(bet.outcome === "AWAY" && "text-brand-400 font-semibold")}>
          Ext. {formatPercent(bet.prob_away)}
        </span>
        <span className="ml-auto">
          Confiance {formatPercent(bet.confidence)}
        </span>
      </div>
    </Link>
  );
}
