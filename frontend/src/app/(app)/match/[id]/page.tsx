"use client";

import { useQuery } from "@tanstack/react-query";
import { useParams, useRouter } from "next/navigation";
import { format } from "date-fns";
import { fr } from "date-fns/locale";
import { ArrowLeft, TrendingUp, Info } from "lucide-react";
import { matchesApi, betsApi } from "@/lib/api";
import { useAuthStore } from "@/store/auth";
import { formatCurrency, formatPercent, outcomeLabel, cn } from "@/lib/utils";
import { useState } from "react";
import type { MatchSummary, Prediction, Recommendation } from "@/types/api";

export default function MatchPage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const user = useAuthStore((s) => s.user);
  const [placing, setPlacing] = useState(false);
  const [betAmount, setBetAmount] = useState("");
  const [bookmaker, setBookmaker] = useState("");
  const [placed, setPlaced] = useState(false);

  const { data: analysis, isLoading } = useQuery<{
    match: MatchSummary;
    prediction: Prediction | null;
    recommendation: Recommendation | null;
    home_form: Record<string, unknown>;
    away_form: Record<string, unknown>;
    h2h: Record<string, unknown>;
    value_assessment: Record<string, unknown>;
  }>({
    queryKey: ["analysis", id],
    queryFn: () => matchesApi.analysis(id).then((r) => r.data),
  });

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin w-8 h-8 border-2 border-brand-500 border-t-transparent rounded-full" />
      </div>
    );
  }

  if (!analysis) {
    return (
      <div className="text-center py-20">
        <p className="text-gray-400">Match introuvable</p>
        <button onClick={() => router.back()} className="btn-secondary mt-4">
          Retour
        </button>
      </div>
    );
  }

  const { match, prediction, recommendation } = analysis;

  async function placeBet() {
    if (!recommendation || !betAmount) return;
    setPlacing(true);
    try {
      await betsApi.create({
        match_id: match.id,
        recommendation_id: recommendation.id,
        outcome: recommendation.outcome,
        amount: parseFloat(betAmount),
        odds: recommendation.odds,
        bookmaker: bookmaker || undefined,
      });
      setPlaced(true);
    } catch {
      alert("Erreur lors de l'enregistrement du pari");
    } finally {
      setPlacing(false);
    }
  }

  const probBars = [
    { label: "Domicile", value: prediction?.prob_home || 0, color: "bg-brand-500" },
    { label: "Nul", value: prediction?.prob_draw || 0, color: "bg-gray-500" },
    { label: "Extérieur", value: prediction?.prob_away || 0, color: "bg-purple-500" },
  ];

  return (
    <div className="space-y-6 max-w-3xl">
      <button
        onClick={() => router.back()}
        className="flex items-center gap-2 text-gray-400 hover:text-gray-100 transition-colors text-sm"
      >
        <ArrowLeft className="w-4 h-4" />
        Retour
      </button>

      {/* Header match */}
      <div className="card">
        <div className="text-xs text-gray-500 mb-3">
          {match.league} · {format(new Date(match.match_date), "EEEE d MMMM yyyy à HH:mm", { locale: fr })}
        </div>
        <div className="flex items-center justify-between">
          <div className="flex-1 text-center">
            <p className="text-xl font-bold">{match.home_team}</p>
            <p className="text-sm text-gray-400 mt-1">Domicile</p>
          </div>
          <div className="text-3xl font-bold text-gray-600 px-8">VS</div>
          <div className="flex-1 text-center">
            <p className="text-xl font-bold">{match.away_team}</p>
            <p className="text-sm text-gray-400 mt-1">Extérieur</p>
          </div>
        </div>
        <div className="flex justify-between mt-4 pt-4 border-t border-gray-800 text-sm text-center">
          {[
            { label: "Cote 1", value: match.home_odds?.toFixed(2) },
            { label: "Cote N", value: match.draw_odds?.toFixed(2) },
            { label: "Cote 2", value: match.away_odds?.toFixed(2) },
          ].map((c) => (
            <div key={c.label}>
              <span className="text-gray-500">{c.label}</span>
              <div className="font-bold text-lg mt-0.5">{c.value || "—"}</div>
            </div>
          ))}
        </div>
      </div>

      {/* Prédictions IA */}
      {prediction && (
        <div className="card">
          <div className="flex items-center gap-2 mb-5">
            <TrendingUp className="w-4 h-4 text-brand-500" />
            <h2 className="font-semibold">Prédictions IA</h2>
            <span className="text-xs text-gray-500 ml-auto">
              v{prediction.model_version} · confiance {formatPercent(prediction.confidence)}
            </span>
          </div>
          <div className="space-y-3">
            {probBars.map((bar) => (
              <div key={bar.label}>
                <div className="flex justify-between text-sm mb-1.5">
                  <span className="text-gray-400">{bar.label}</span>
                  <span className="font-semibold">{formatPercent(bar.value)}</span>
                </div>
                <div className="h-2.5 bg-gray-800 rounded-full overflow-hidden">
                  <div
                    className={cn("h-full rounded-full transition-all", bar.color)}
                    style={{ width: formatPercent(bar.value) }}
                  />
                </div>
              </div>
            ))}
          </div>

          {/* SHAP */}
          {prediction.shap_values && Object.keys(prediction.shap_values).length > 0 && (
            <div className="mt-5 pt-5 border-t border-gray-800">
              <p className="text-sm font-medium mb-3 flex items-center gap-1">
                <Info className="w-3.5 h-3.5 text-gray-500" />
                Facteurs clés (SHAP)
              </p>
              <div className="space-y-1.5">
                {Object.entries(prediction.shap_values ?? {})
                  .sort(([, a], [, b]) => Math.abs(b) - Math.abs(a))
                  .slice(0, 6)
                  .map(([feature, value]) => (
                    <div key={feature} className="flex items-center justify-between text-xs">
                      <span className="text-gray-400 font-mono">{feature}</span>
                      <span className={value > 0 ? "text-edge-green" : "text-edge-red"}>
                        {value > 0 ? "+" : ""}{value.toFixed(3)}
                      </span>
                    </div>
                  ))}
              </div>
            </div>
          )}
        </div>
      )}

      {/* Recommandation Kelly */}
      {recommendation && user?.plan !== "FREE" && (
        <div className="card border-brand-500/30 bg-brand-500/5">
          <div className="flex items-center justify-between mb-4">
            <h2 className="font-semibold text-brand-400">Recommandation Kelly</h2>
            <span className={cn(
              "badge-edge",
              recommendation.edge >= 0.08 ? "badge-high" : "badge-medium"
            )}>
              Edge {(recommendation.edge * 100).toFixed(1)}%
            </span>
          </div>
          <div className="grid grid-cols-3 gap-4 text-center mb-5">
            <div>
              <p className="text-xs text-gray-500">Outcome</p>
              <p className="font-bold mt-1">{outcomeLabel(recommendation.outcome)}</p>
            </div>
            <div>
              <p className="text-xs text-gray-500">Mise conseillée</p>
              <p className="font-bold text-brand-400 mt-1">
                {recommendation.recommended_amount != null ? formatCurrency(recommendation.recommended_amount) : "—"}
              </p>
            </div>
            <div>
              <p className="text-xs text-gray-500">Kelly fraction</p>
              <p className="font-bold mt-1">{recommendation.kelly_stake != null ? formatPercent(recommendation.kelly_stake) : "—"}</p>
            </div>
          </div>

          {placed ? (
            <div className="p-3 rounded-lg bg-edge-green/10 border border-edge-green/20 text-edge-green text-sm text-center">
              ✓ Pari enregistré dans votre historique
            </div>
          ) : (
            <div className="space-y-3">
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="text-xs text-gray-500 block mb-1">Mise réelle (€)</label>
                  <input
                    type="number"
                    className="input"
                    placeholder={recommendation.recommended_amount?.toFixed(0) ?? ""}
                    value={betAmount}
                    onChange={(e) => setBetAmount(e.target.value)}
                    min="1"
                    step="1"
                  />
                </div>
                <div>
                  <label className="text-xs text-gray-500 block mb-1">Bookmaker</label>
                  <input
                    type="text"
                    className="input"
                    placeholder="Betclic, Winamax..."
                    value={bookmaker}
                    onChange={(e) => setBookmaker(e.target.value)}
                  />
                </div>
              </div>
              <button
                onClick={placeBet}
                disabled={placing || !betAmount}
                className="btn-primary w-full"
              >
                {placing ? "Enregistrement..." : "Enregistrer ce pari"}
              </button>
            </div>
          )}

          {recommendation.strategy && (
            <p className="text-xs text-gray-500 mt-3 pt-3 border-t border-gray-800/50">
              {recommendation.strategy}
            </p>
          )}
        </div>
      )}

      {user?.plan === "FREE" && (
        <div className="card text-center py-8 border-brand-500/20">
          <p className="text-gray-300 mb-2">Recommandations Kelly réservées aux abonnés Pro</p>
          <p className="text-sm text-gray-500 mb-4">
            Passez à Pro pour voir la mise optimale calculée automatiquement.
          </p>
          <a href="/settings" className="btn-primary">Passer à Pro — 19€/mois</a>
        </div>
      )}
    </div>
  );
}
