"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useParams, useRouter } from "next/navigation";
import { format } from "date-fns";
import { fr } from "date-fns/locale";
import { ArrowLeft, TrendingUp, Info, HelpCircle } from "lucide-react";
import { matchesApi, betsApi } from "@/lib/api";
import { useAuthStore } from "@/store/auth";
import { formatCurrency, formatPercent, outcomeLabel, pickedTeamLabel, cn } from "@/lib/utils";
import { useState } from "react";
import type { MatchSummary, Prediction, Recommendation } from "@/types/api";
import ExplainModal from "@/components/ExplainModal";

export default function MatchPage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const qc = useQueryClient();
  const { user, updateUser } = useAuthStore();
  const [placing, setPlacing] = useState(false);
  const [betAmount, setBetAmount] = useState("");
  const [bookmaker, setBookmaker] = useState("");
  const [placed, setPlaced] = useState(false);
  const [explainOpen, setExplainOpen] = useState(false);

  const { data: analysis, isLoading } = useQuery<{
    match: MatchSummary;
    prediction: Prediction | null;
    recommendation: Recommendation | null;
    ou_recommendation: Recommendation | null;
    ah_recommendation: (Recommendation & { ah_line?: number; team_name?: string; handicap?: string }) | null;
    league_whitelisted?: boolean;
    ou_whitelisted?: boolean;
    ah_whitelisted?: boolean;
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
      const amount = parseFloat(betAmount);
      await betsApi.create({
        match_id: match.id,
        recommendation_id: recommendation.id,
        outcome: recommendation.outcome,
        amount,
        odds: recommendation.odds,
        bookmaker: bookmaker || undefined,
      });
      // Rafraîchir la bankroll et les paris dans tout le dashboard
      qc.invalidateQueries({ queryKey: ["bankroll"] });
      qc.invalidateQueries({ queryKey: ["bets"] });
      // Mettre à jour le store local
      if (user) updateUser({ bankroll: Math.max(0, user.bankroll - amount) });
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
          <div className="flex items-center gap-2 mb-3">
            <TrendingUp className="w-4 h-4 text-brand-500" />
            <h2 className="font-semibold">Prédictions IA</h2>
            <span className="text-xs text-gray-500 ml-auto">
              v{prediction.model_version} · confiance {formatPercent(prediction.confidence)}
            </span>
          </div>
          {/* Bouton Expliquer — toujours visible, full width sur mobile */}
          <button
            type="button"
            onClick={() => setExplainOpen(true)}
            className="mb-5 w-full sm:w-auto inline-flex items-center justify-center gap-2 px-3 py-1.5 rounded-md bg-brand-500/10 border border-brand-500/30 text-sm text-brand-300 hover:bg-brand-500/20 hover:border-brand-500/50 transition"
            title="Pourquoi le modèle prédit ça ?"
          >
            <HelpCircle className="w-4 h-4" />
            Expliquer la prédiction
          </button>
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

      {/* World Cup : message dédié (modèle non validé sur foot international) */}
      {analysis?.match.league === "World Cup" && user?.plan !== "FREE" && (
        <div className="card border-blue-500/30 bg-blue-500/5">
          <div className="flex items-start gap-3">
            <Info className="w-5 h-5 text-blue-400 shrink-0 mt-0.5" />
            <div className="text-sm">
              <p className="font-semibold text-blue-300">
                Match de Coupe du Monde
              </p>
              <p className="text-gray-300 mt-1">
                Notre modèle est entraîné sur 18 000 matchs de clubs européens. Le foot
                international (équipes nationales) a une dynamique très différente :
                joueurs qui se retrouvent rarement, pas de "forme" continue, importance
                énorme des blessures/forfaits. <strong>Aucune mise n'est conseillée</strong> sur la WC.
              </p>
            </div>
          </div>
        </div>
      )}

      {/* Ligue non whitelistée : pas de reco */}
      {!recommendation && analysis?.league_whitelisted === false && analysis?.match.league !== "World Cup" && analysis.prediction && user?.plan !== "FREE" && (
        <div className="card border-yellow-500/30 bg-yellow-500/5">
          <div className="flex items-start gap-3">
            <Info className="w-5 h-5 text-yellow-400 shrink-0 mt-0.5" />
            <div className="text-sm">
              <p className="font-semibold text-yellow-300">
                Pas de recommandation pour cette ligue
              </p>
              <p className="text-gray-300 mt-1">
                Selon le backtest historique, le modèle n'est pas rentable sur {analysis.match.league}.
                Les prédictions s'affichent à titre indicatif mais aucune mise n'est conseillée.
              </p>
            </div>
          </div>
        </div>
      )}

      {/* Ligue whitelistée MAIS aucun value bet trouvé (edges hors range ou pas d'edge) */}
      {!recommendation && analysis?.league_whitelisted === true && analysis.prediction && user?.plan !== "FREE" && (() => {
        const m = analysis.match;
        const p = analysis.prediction;
        const edges = [
          { label: m.home_team, prob: p.prob_home, odds: m.home_odds, side: "HOME" },
          { label: "Match nul", prob: p.prob_draw, odds: m.draw_odds, side: "DRAW" },
          { label: m.away_team, prob: p.prob_away, odds: m.away_odds, side: "AWAY" },
        ].map((e) => ({
          ...e,
          edge: e.odds ? e.prob * e.odds - 1 : null,
        }));
        const maxEdge = Math.max(...edges.map((e) => e.edge ?? -1));
        const hasHighEdge = maxEdge > 0.20;
        const hasLowEdge = maxEdge >= 0 && maxEdge < 0.08;
        return (
          <div className="card border-gray-700 bg-gray-900/40">
            <div className="flex items-start gap-3 mb-3">
              <Info className="w-5 h-5 text-gray-400 shrink-0 mt-0.5" />
              <div className="text-sm">
                <p className="font-semibold text-gray-200">Pas de value bet sur ce match</p>
                <p className="text-gray-400 mt-1">
                  {hasHighEdge
                    ? `Le modèle détecte un edge énorme (${(maxEdge * 100).toFixed(0)}%) mais on filtre au-delà de 20% car ces "value bets" géants sont rarement réels (modèle mal calibré sur les outsiders extrêmes — confirmé par le backtest).`
                    : hasLowEdge
                    ? `Edge maximum ${(maxEdge * 100).toFixed(1)}%, sous le seuil minimum de 8% (où la stratégie a une vraie rentabilité historique).`
                    : "Le modèle est moins confiant que le bookmaker sur tous les outcomes — pas d'avantage exploitable."}
                </p>
              </div>
            </div>
            <div className="grid grid-cols-3 gap-2 text-xs">
              {edges.map((e) => (
                <div key={e.side} className="bg-gray-800/60 rounded p-2 text-center">
                  <p className="text-gray-500 truncate">{e.label}</p>
                  <p className="font-semibold mt-1">{(e.prob * 100).toFixed(0)}% · {e.odds?.toFixed(2) ?? "—"}</p>
                  {e.edge !== null && (
                    <p className={cn(
                      "text-[10px] font-mono",
                      e.edge >= 0.08 && e.edge <= 0.20 ? "text-green-400" :
                      e.edge > 0.20 ? "text-yellow-400" :
                      e.edge < 0 ? "text-red-400" : "text-gray-500"
                    )}>
                      Edge {e.edge >= 0 ? "+" : ""}{(e.edge * 100).toFixed(0)}%
                    </p>
                  )}
                </div>
              ))}
            </div>
          </div>
        );
      })()}

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
              <p className="text-xs text-gray-500">Pari sur</p>
              <p className="font-bold mt-1 truncate">
                {pickedTeamLabel(recommendation.outcome, analysis.match)}
              </p>
              <p className="text-[10px] text-gray-500">({outcomeLabel(recommendation.outcome)})</p>
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
              ✓ Pari sur <strong>{pickedTeamLabel(recommendation.outcome, analysis.match)}</strong> enregistré
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
                {placing
                  ? "Enregistrement..."
                  : `Enregistrer ce pari sur ${pickedTeamLabel(recommendation.outcome, analysis.match)}`}
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

      {/* Recommandation O/U 2.5 buts */}
      {analysis?.ou_recommendation && user?.plan !== "FREE" && (
        <div className="card border-purple-500/30 bg-purple-500/5">
          <div className="flex items-center justify-between mb-4">
            <h2 className="font-semibold text-purple-300 flex items-center gap-2">
              Recommandation O/U 2.5 buts
              <span className="text-[10px] px-1.5 py-0.5 rounded bg-purple-500/20 text-purple-200">
                Buts
              </span>
            </h2>
            <span className={cn(
              "badge-edge",
              analysis.ou_recommendation.edge >= 0.10 ? "badge-high" : "badge-medium"
            )}>
              Edge {(analysis.ou_recommendation.edge * 100).toFixed(1)}%
            </span>
          </div>
          <div className="grid grid-cols-3 gap-4 text-center">
            <div>
              <p className="text-xs text-gray-500">Pari sur</p>
              <p className="font-bold mt-1">
                {analysis.ou_recommendation.outcome === "OVER" ? "Plus de 2.5 buts" : "Moins de 2.5 buts"}
              </p>
            </div>
            <div>
              <p className="text-xs text-gray-500">Mise conseillée</p>
              <p className="font-bold text-purple-300 mt-1">
                {formatCurrency(analysis.ou_recommendation.recommended_amount)}
              </p>
            </div>
            <div>
              <p className="text-xs text-gray-500">Cote</p>
              <p className="font-bold mt-1">{analysis.ou_recommendation.odds?.toFixed(2) ?? "—"}</p>
            </div>
          </div>
          {analysis.ou_recommendation.strategy && (
            <p className="text-xs text-gray-500 mt-3 pt-3 border-t border-gray-800/50">
              {analysis.ou_recommendation.strategy}
            </p>
          )}
        </div>
      )}

      {/* Recommandation Asian Handicap */}
      {analysis?.ah_recommendation && user?.plan !== "FREE" && (
        <div className="card border-teal-500/30 bg-teal-500/5">
          <div className="flex items-center justify-between mb-4">
            <h2 className="font-semibold text-teal-300 flex items-center gap-2">
              Recommandation Asian Handicap
              <span className="text-[10px] px-1.5 py-0.5 rounded bg-teal-500/20 text-teal-200">
                Spread
              </span>
            </h2>
            <span className={cn(
              "badge-edge",
              analysis.ah_recommendation.edge >= 0.10 ? "badge-high" : "badge-medium"
            )}>
              Edge {(analysis.ah_recommendation.edge * 100).toFixed(1)}%
            </span>
          </div>
          <div className="grid grid-cols-3 gap-4 text-center">
            <div>
              <p className="text-xs text-gray-500">Pari sur</p>
              <p className="font-bold mt-1 truncate">
                {analysis.ah_recommendation.team_name ?? "—"}
              </p>
              <p className="text-[10px] text-gray-500">handicap {analysis.ah_recommendation.handicap}</p>
            </div>
            <div>
              <p className="text-xs text-gray-500">Mise conseillée</p>
              <p className="font-bold text-teal-300 mt-1">
                {formatCurrency(analysis.ah_recommendation.recommended_amount)}
              </p>
            </div>
            <div>
              <p className="text-xs text-gray-500">Cote</p>
              <p className="font-bold mt-1">{analysis.ah_recommendation.odds?.toFixed(2) ?? "—"}</p>
            </div>
          </div>
          {analysis.ah_recommendation.strategy && (
            <p className="text-xs text-gray-500 mt-3 pt-3 border-t border-gray-800/50">
              {analysis.ah_recommendation.strategy}
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

      {explainOpen && (
        <ExplainModal
          matchId={match.id}
          homeTeam={match.home_team}
          awayTeam={match.away_team}
          onClose={() => setExplainOpen(false)}
        />
      )}
    </div>
  );
}
