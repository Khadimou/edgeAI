"use client";

import { useQuery } from "@tanstack/react-query";
import { X, TrendingUp, Calendar, Info } from "lucide-react";
import { adminApi } from "@/lib/api";
import { cn } from "@/lib/utils";

interface ExplainData {
  match: {
    id: string;
    home_team: string;
    away_team: string;
    match_date: string | null;
    league: string;
    status: string;
    score: string | null;
  };
  odds: {
    "1x2": { home: number | null; draw: number | null; away: number | null };
    ou_2_5: { over: number | null; under: number | null };
    ah: { line: number | null; home: number | null; away: number | null };
  };
  market_implied_1x2: {
    home: number;
    draw: number;
    away: number;
    margin: number;
  } | null;
  prediction: {
    prob_home: number | null;
    prob_draw: number | null;
    prob_away: number | null;
    prob_over_25: number | null;
    prob_under_25: number | null;
    prob_ah_home: number | null;
    prob_ah_away: number | null;
    confidence: number | null;
    model_version: string | null;
    computed_at: string | null;
  };
  shap_top_features: { feature: string; contribution: number }[];
  home_recent_form: {
    date: string | null;
    venue: "H" | "A";
    opponent: string;
    score: string;
    result: "W" | "D" | "L";
  }[];
  away_recent_form: {
    date: string | null;
    venue: "H" | "A";
    opponent: string;
    score: string;
    result: "W" | "D" | "L";
  }[];
}

interface Props {
  matchId: string;
  homeTeam: string;
  awayTeam: string;
  onClose: () => void;
}

const RESULT_COLOR: Record<string, string> = {
  W: "text-edge-green",
  D: "text-gray-400",
  L: "text-edge-red",
};

export default function ExplainModal({ matchId, homeTeam, awayTeam, onClose }: Props) {
  const { data, isLoading, error } = useQuery<ExplainData>({
    queryKey: ["explain", matchId],
    queryFn: () => adminApi.explain(matchId).then((r) => r.data),
  });

  const pct = (v: number | null | undefined) =>
    v != null ? `${(v * 100).toFixed(1)}%` : "—";

  // Côté modèle vs côté marché (1X2)
  const compareRow = (
    label: string,
    modelProb: number | null | undefined,
    marketProb: number | null | undefined,
  ) => {
    const diff = modelProb != null && marketProb != null ? modelProb - marketProb : null;
    return (
      <div className="grid grid-cols-3 items-center text-sm py-1.5">
        <span className="text-gray-400">{label}</span>
        <div className="flex items-center justify-end gap-3">
          <span className="text-gray-500">marché</span>
          <span className="text-gray-300 font-mono w-12 text-right">{pct(marketProb)}</span>
        </div>
        <div className="flex items-center justify-end gap-3">
          <span className="text-brand-400 font-mono w-12 text-right">{pct(modelProb)}</span>
          {diff != null && (
            <span
              className={cn(
                "text-xs font-mono w-12 text-right",
                diff > 0.05
                  ? "text-edge-green"
                  : diff < -0.05
                    ? "text-edge-red"
                    : "text-gray-500",
              )}
            >
              {diff > 0 ? "+" : ""}
              {(diff * 100).toFixed(1)}
            </span>
          )}
        </div>
      </div>
    );
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-sm p-4"
      onClick={onClose}
    >
      <div
        className="bg-gray-900 border border-gray-800 rounded-xl max-w-3xl w-full max-h-[90vh] overflow-y-auto"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="sticky top-0 bg-gray-900 border-b border-gray-800 px-5 py-4 flex items-center justify-between z-10">
          <div>
            <h2 className="font-semibold">Explication du modèle</h2>
            <p className="text-xs text-gray-500 mt-0.5">
              {homeTeam} vs {awayTeam}
            </p>
          </div>
          <button
            onClick={onClose}
            className="text-gray-400 hover:text-gray-200 p-1"
            aria-label="Fermer"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Body */}
        <div className="p-5 space-y-6">
          {isLoading && (
            <div className="flex items-center justify-center py-12">
              <div className="animate-spin w-6 h-6 border-2 border-brand-500 border-t-transparent rounded-full" />
            </div>
          )}

          {error && (
            <p className="text-edge-red text-sm">
              Erreur de chargement. L&apos;endpoint d&apos;explication n&apos;est peut-être pas
              encore déployé.
            </p>
          )}

          {data && (
            <>
              {/* Comparaison Modèle vs Marché */}
              {data.market_implied_1x2 && (
                <section>
                  <h3 className="font-semibold text-sm mb-3 flex items-center gap-2">
                    <TrendingUp className="w-4 h-4 text-brand-500" />
                    Modèle vs Marché — 1X2
                  </h3>
                  <div className="bg-gray-800/50 rounded-lg p-3">
                    <div className="grid grid-cols-3 items-center text-xs text-gray-500 pb-2 border-b border-gray-700/50">
                      <span></span>
                      <span className="text-right">Probabilité</span>
                      <span className="text-right">Modèle (delta)</span>
                    </div>
                    {compareRow(
                      "Domicile",
                      data.prediction.prob_home,
                      data.market_implied_1x2.home,
                    )}
                    {compareRow(
                      "Nul",
                      data.prediction.prob_draw,
                      data.market_implied_1x2.draw,
                    )}
                    {compareRow(
                      "Extérieur",
                      data.prediction.prob_away,
                      data.market_implied_1x2.away,
                    )}
                    <p className="text-xs text-gray-500 mt-2 pt-2 border-t border-gray-700/50">
                      Marge bookmaker : {pct(data.market_implied_1x2.margin)} · Cotes :{" "}
                      {data.odds["1x2"].home?.toFixed(2)} /{" "}
                      {data.odds["1x2"].draw?.toFixed(2)} /{" "}
                      {data.odds["1x2"].away?.toFixed(2)}
                    </p>
                  </div>
                </section>
              )}

              {/* SHAP — features qui poussent le modèle */}
              {data.shap_top_features.length > 0 && (
                <section>
                  <h3 className="font-semibold text-sm mb-3 flex items-center gap-2">
                    <Info className="w-4 h-4 text-brand-500" />
                    Features les plus contributives (SHAP)
                  </h3>
                  <p className="text-xs text-gray-500 mb-2">
                    Positif = pousse vers Domicile ; négatif = pousse vers Extérieur/Nul
                  </p>
                  <div className="bg-gray-800/50 rounded-lg overflow-hidden">
                    {data.shap_top_features.map((f, i) => (
                      <div
                        key={f.feature}
                        className={cn(
                          "flex items-center justify-between text-xs px-3 py-2",
                          i < data.shap_top_features.length - 1 &&
                            "border-b border-gray-700/30",
                        )}
                      >
                        <span className="text-gray-300 font-mono">{f.feature}</span>
                        <div className="flex items-center gap-2">
                          <div className="w-24 h-1 bg-gray-700 rounded-full relative overflow-hidden">
                            <div
                              className={cn(
                                "absolute top-0 h-full",
                                f.contribution > 0
                                  ? "left-1/2 bg-edge-green"
                                  : "right-1/2 bg-edge-red",
                              )}
                              style={{
                                width: `${Math.min(50, Math.abs(f.contribution) * 200)}%`,
                              }}
                            />
                            <div className="absolute left-1/2 top-0 w-px h-full bg-gray-600" />
                          </div>
                          <span
                            className={cn(
                              "font-mono w-14 text-right",
                              f.contribution > 0 ? "text-edge-green" : "text-edge-red",
                            )}
                          >
                            {f.contribution > 0 ? "+" : ""}
                            {f.contribution.toFixed(3)}
                          </span>
                        </div>
                      </div>
                    ))}
                  </div>
                </section>
              )}

              {/* Forme récente */}
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                <FormBlock title={`${homeTeam} (5 derniers)`} matches={data.home_recent_form} />
                <FormBlock title={`${awayTeam} (5 derniers)`} matches={data.away_recent_form} />
              </div>

              {/* Footer technique */}
              <div className="text-xs text-gray-500 pt-3 border-t border-gray-800 flex items-center gap-2">
                <Calendar className="w-3 h-3" />
                Modèle {data.prediction.model_version} · prédit le{" "}
                {data.prediction.computed_at
                  ? new Date(data.prediction.computed_at).toLocaleString("fr-FR")
                  : "?"}
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

function FormBlock({
  title,
  matches,
}: {
  title: string;
  matches: ExplainData["home_recent_form"];
}) {
  if (!matches.length)
    return (
      <div>
        <h4 className="font-medium text-sm mb-2">{title}</h4>
        <p className="text-xs text-gray-500">Pas d&apos;historique</p>
      </div>
    );
  return (
    <div>
      <h4 className="font-medium text-sm mb-2 flex items-center gap-2">
        {title}
        <span className="ml-auto flex gap-1">
          {matches.map((m, i) => (
            <span
              key={i}
              className={cn(
                "w-5 h-5 rounded text-[10px] font-bold flex items-center justify-center",
                m.result === "W"
                  ? "bg-edge-green/20 text-edge-green"
                  : m.result === "D"
                    ? "bg-gray-700/50 text-gray-300"
                    : "bg-edge-red/20 text-edge-red",
              )}
            >
              {m.result}
            </span>
          ))}
        </span>
      </h4>
      <div className="bg-gray-800/50 rounded-lg overflow-hidden">
        {matches.map((m, i) => (
          <div
            key={i}
            className={cn(
              "flex items-center justify-between text-xs px-3 py-1.5",
              i < matches.length - 1 && "border-b border-gray-700/30",
            )}
          >
            <span className="text-gray-400">
              <span className="font-mono text-[10px] mr-1.5 text-gray-600">
                {m.venue}
              </span>
              {m.opponent}
            </span>
            <div className="flex items-center gap-2">
              <span className="text-gray-300 font-mono">{m.score}</span>
              <span className={cn("font-bold w-3", RESULT_COLOR[m.result])}>
                {m.result}
              </span>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

