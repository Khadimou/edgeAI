"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { format } from "date-fns";
import { fr } from "date-fns/locale";
import { Target, ChevronRight, Lock } from "lucide-react";
import { matchesApi, recsApi, bankrollApi } from "@/lib/api";
import { useAuthStore } from "@/store/auth";
import { formatCurrency, formatPercent, outcomeLabel, cn } from "@/lib/utils";
import type { MatchSummary, Recommendation, BankrollStats } from "@/types/api";

export default function DashboardPage() {
  const user = useAuthStore((s) => s.user);
  const isPro = user?.plan !== "FREE";

  const { data: matchesData } = useQuery<MatchSummary[]>({
    queryKey: ["matches"],
    queryFn: () => matchesApi.upcoming(undefined, 10).then((r) => r.data),
  });

  const { data: recsData } = useQuery<Recommendation[]>({
    queryKey: ["recommendations"],
    queryFn: () =>
      isPro
        ? recsApi.list(5).then((r) => r.data)
        : recsApi.preview().then((r) => r.data),
  });

  const { data: bankroll } = useQuery<BankrollStats>({
    queryKey: ["bankroll"],
    queryFn: () => bankrollApi.history().then((r) => r.data),
  });

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-2xl font-bold">Dashboard</h1>
        <p className="text-gray-400 mt-1">
          {format(new Date(), "EEEE d MMMM yyyy", { locale: fr })}
        </p>
      </div>

      {/* KPIs */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        <div className="stat-card">
          <div className="stat-label">Bankroll</div>
          <div className="stat-value text-brand-400">
            {formatCurrency(user?.bankroll ?? 0)}
          </div>
        </div>
        <div className="stat-card">
          <div className="stat-label">ROI total</div>
          <div className={cn("stat-value", (bankroll?.roi_percent ?? 0) >= 0 ? "text-edge-green" : "text-edge-red")}>
            {bankroll ? `${bankroll.roi_percent > 0 ? "+" : ""}${bankroll.roi_percent.toFixed(1)}%` : "—"}
          </div>
        </div>
        <div className="stat-card">
          <div className="stat-label">P&L total</div>
          <div className={cn("stat-value", (bankroll?.total_profit_loss ?? 0) >= 0 ? "text-edge-green" : "text-edge-red")}>
            {bankroll ? formatCurrency(bankroll.total_profit_loss) : "—"}
          </div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Opportunités</div>
          <div className="stat-value text-yellow-400">{recsData?.length ?? 0}</div>
        </div>
      </div>

      {/* Top recommandations */}
      <section>
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-semibold">
            {isPro ? "Top opportunités" : "Aperçu des opportunités"}
          </h2>
          {!isPro && (
            <Link href="/settings" className="flex items-center gap-1 text-sm text-brand-400 hover:underline">
              <Lock className="w-3 h-3" />
              Passer Pro
            </Link>
          )}
        </div>

        {!recsData?.length ? (
          <div className="card text-center py-10">
            <Target className="w-10 h-10 text-gray-600 mx-auto mb-3" />
            <p className="text-gray-400">Aucune opportunité détectée pour le moment.</p>
            <p className="text-sm text-gray-500 mt-1">Le modèle analyse les matchs toutes les 6h.</p>
          </div>
        ) : (
          <div className="space-y-3">
            {recsData.map((rec, i) => (
              <RecommendationCard key={i} rec={rec} isPro={isPro} />
            ))}
          </div>
        )}
      </section>

      {/* Matchs à venir */}
      <section>
        <h2 className="text-lg font-semibold mb-4">Matchs analysés (48h)</h2>
        <div className="space-y-2">
          {matchesData?.slice(0, 8).map((match) => {
            const pred = match.prediction;
            return (
              <Link
                key={match.id}
                href={`/match/${match.id}`}
                className="card flex items-center justify-between hover:border-gray-700 transition-colors group"
              >
                <div>
                  <p className="font-medium">
                    {match.home_team} <span className="text-gray-500">vs</span> {match.away_team}
                  </p>
                  <p className="text-xs text-gray-500 mt-0.5">
                    {match.league} · {format(new Date(match.match_date), "EEE d MMM HH:mm", { locale: fr })}
                  </p>
                </div>
                <div className="flex items-center gap-3">
                  {pred && (
                    <div className="text-right text-sm">
                      <span className="text-gray-400">
                        {formatPercent(pred.prob_home)} ·{" "}
                        {formatPercent(pred.prob_draw)} ·{" "}
                        {formatPercent(pred.prob_away)}
                      </span>
                    </div>
                  )}
                  <ChevronRight className="w-4 h-4 text-gray-600 group-hover:text-gray-400 transition-colors" />
                </div>
              </Link>
            );
          })}
        </div>
      </section>
    </div>
  );
}

function RecommendationCard({ rec, isPro }: { rec: Recommendation; isPro: boolean }) {
  return (
    <div className="card hover:border-gray-700 transition-colors">
      <div className="flex items-start justify-between gap-4">
        <div className="flex-1">
          <div className="flex items-center gap-2 mb-1">
            <span className={cn(
              "badge-edge",
              rec.edge >= 0.08 ? "badge-high" : rec.edge >= 0.04 ? "badge-medium" : "badge-low"
            )}>
              Edge {(rec.edge * 100).toFixed(1)}%
            </span>
            <span className="text-sm font-medium">{outcomeLabel(rec.outcome)}</span>
          </div>
          <p className="font-semibold">
            {rec.home_team} vs {rec.away_team}
          </p>
          <p className="text-xs text-gray-500 mt-0.5">
            {rec.league} · {format(new Date(rec.match_date), "EEE d MMM HH:mm", { locale: fr })}
          </p>
        </div>
        <div className="text-right">
          <p className="text-sm text-gray-400">Cote</p>
          <p className="font-bold text-lg">{rec.odds.toFixed(2)}</p>
          {isPro && rec.recommended_amount != null ? (
            <>
              <p className="text-xs text-gray-500 mt-1">Mise conseillée</p>
              <p className="font-semibold text-brand-400">{formatCurrency(rec.recommended_amount)}</p>
            </>
          ) : (
            <span className="text-xs text-gray-500 flex items-center gap-1 justify-end mt-1">
              <Lock className="w-3 h-3" /> Pro
            </span>
          )}
        </div>
      </div>
      {isPro && rec.strategy && (
        <p className="text-xs text-gray-500 mt-3 pt-3 border-t border-gray-800">{rec.strategy}</p>
      )}
    </div>
  );
}
