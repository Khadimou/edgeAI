"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip,
  ResponsiveContainer, Legend,
} from "recharts";
import { Activity, CheckCircle2, AlertTriangle, AlertCircle, Brain, TrendingUp, TrendingDown, Calendar } from "lucide-react";
import { modelApi } from "@/lib/api";
import { cn } from "@/lib/utils";

interface Metrics {
  n: number;
  accuracy: number | null;
  log_loss: number | null;
  brier_score: number | null;
  home_accuracy: number | null;
  draw_accuracy: number | null;
  away_accuracy: number | null;
  outcome_distribution?: { home_pct: number; draw_pct: number; away_pct: number };
}

interface VersionMetrics extends Metrics {
  model_version: string;
}

interface DailyMetrics extends Metrics {
  date: string;
}

interface DeployedModel {
  version: string;
  oof_accuracy: number;
  oof_log_loss: number;
  oof_brier_score: number;
  trained_at: string | null;
}

interface Drift {
  live_log_loss: number;
  live_accuracy: number;
  log_loss_delta: number;
  accuracy_delta: number;
  n_samples: number;
  status: "healthy" | "warning" | "degraded" | "insufficient_data";
}

interface PerformanceData {
  overall: Metrics & { window_days: number; latest_match_date: string | null };
  by_version: VersionMetrics[];
  daily: DailyMetrics[];
  deployed_model: DeployedModel | null;
  drift: Drift | null;
}

const DAY_OPTIONS = [
  { value: 7, label: "7 jours" },
  { value: 30, label: "30 jours" },
  { value: 90, label: "90 jours" },
  { value: 365, label: "1 an" },
];

function StatusBadge({ status }: { status: Drift["status"] }) {
  const config = {
    healthy: {
      icon: CheckCircle2,
      label: "Sain",
      className: "bg-green-500/20 text-green-400 border-green-500/30",
    },
    warning: {
      icon: AlertTriangle,
      label: "Dérive légère",
      className: "bg-yellow-500/20 text-yellow-400 border-yellow-500/30",
    },
    degraded: {
      icon: AlertCircle,
      label: "Dégradé",
      className: "bg-red-500/20 text-red-400 border-red-500/30",
    },
    insufficient_data: {
      icon: Activity,
      label: "Données insuffisantes",
      className: "bg-gray-700 text-gray-400 border-gray-600",
    },
  }[status];
  const Icon = config.icon;
  return (
    <span className={cn("inline-flex items-center gap-1.5 text-xs px-2.5 py-1 rounded-full border font-semibold", config.className)}>
      <Icon className="w-3.5 h-3.5" />
      {config.label}
    </span>
  );
}

function Kpi({ label, value, sub, accent = "default" }: {
  label: string;
  value: string;
  sub?: string;
  accent?: "default" | "good" | "bad";
}) {
  const valueClass = {
    default: "text-white",
    good: "text-green-400",
    bad: "text-red-400",
  }[accent];
  return (
    <div className="bg-gray-900 border border-gray-800 rounded-xl p-4">
      <p className="text-xs text-gray-500 mb-1">{label}</p>
      <p className={cn("text-2xl font-bold", valueClass)}>{value}</p>
      {sub && <p className="text-xs text-gray-500 mt-1">{sub}</p>}
    </div>
  );
}

function DeltaBadge({ delta, invert = false }: { delta: number; invert?: boolean }) {
  // invert=true means lower is better (log_loss)
  const good = invert ? delta < 0 : delta > 0;
  const Icon = good ? TrendingUp : TrendingDown;
  const cls = good ? "text-green-400" : "text-red-400";
  const sign = delta >= 0 ? "+" : "";
  return (
    <span className={cn("inline-flex items-center gap-1 text-xs font-semibold", cls)}>
      <Icon className="w-3 h-3" />
      {sign}{delta.toFixed(4)}
    </span>
  );
}

export default function ModelPage() {
  const [days, setDays] = useState(30);

  const { data, isLoading } = useQuery<PerformanceData>({
    queryKey: ["model-performance", days],
    queryFn: () => modelApi.performance(days).then((r) => r.data),
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

  const { overall, by_version, daily, deployed_model, drift } = data;

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-2xl font-bold flex items-center gap-2">
            <Brain className="w-6 h-6 text-brand-500" />
            Performance du modèle
          </h1>
          <p className="text-sm text-gray-400 mt-1">
            Précision live mesurée sur prédictions vs résultats réels
          </p>
        </div>
        <div className="flex gap-1 bg-gray-900 border border-gray-800 rounded-lg p-1">
          {DAY_OPTIONS.map((opt) => (
            <button
              key={opt.value}
              onClick={() => setDays(opt.value)}
              className={cn(
                "px-3 py-1.5 rounded-md text-xs font-medium transition-colors",
                days === opt.value
                  ? "bg-brand-600 text-white"
                  : "text-gray-400 hover:text-gray-100"
              )}
            >
              {opt.label}
            </button>
          ))}
        </div>
      </div>

      {overall.n === 0 ? (
        <div className="bg-gray-900 border border-gray-800 rounded-xl p-8 text-center">
          <Activity className="w-10 h-10 text-gray-600 mx-auto mb-3" />
          <p className="text-gray-400 font-medium">
            Aucune prédiction évaluée sur cette période
          </p>
          <p className="text-xs text-gray-600 mt-1">
            Les métriques live apparaîtront après les premiers matchs FINISHED avec prédiction.
          </p>
        </div>
      ) : (
        <>
          {/* Drift status */}
          {drift && deployed_model && (
            <div className="bg-gray-900 border border-gray-800 rounded-xl p-5">
              <div className="flex items-center justify-between flex-wrap gap-3 mb-4">
                <div>
                  <p className="text-xs text-gray-500 mb-1">Modèle déployé</p>
                  <p className="text-base font-bold">{deployed_model.version}</p>
                </div>
                <StatusBadge status={drift.status} />
              </div>
              <div className="grid grid-cols-2 md:grid-cols-4 gap-3 text-sm">
                <div>
                  <p className="text-xs text-gray-500">Log-loss live</p>
                  <p className="font-bold">{drift.live_log_loss.toFixed(4)}</p>
                  <div className="mt-0.5"><DeltaBadge delta={drift.log_loss_delta} invert /></div>
                  <p className="text-[10px] text-gray-600 mt-0.5">vs OOF {deployed_model.oof_log_loss.toFixed(4)}</p>
                </div>
                <div>
                  <p className="text-xs text-gray-500">Accuracy live</p>
                  <p className="font-bold">{(drift.live_accuracy * 100).toFixed(1)}%</p>
                  <div className="mt-0.5"><DeltaBadge delta={drift.accuracy_delta} /></div>
                  <p className="text-[10px] text-gray-600 mt-0.5">vs OOF {(deployed_model.oof_accuracy * 100).toFixed(1)}%</p>
                </div>
                <div>
                  <p className="text-xs text-gray-500">Échantillons live</p>
                  <p className="font-bold">{drift.n_samples}</p>
                  <p className="text-[10px] text-gray-600 mt-0.5">matchs FINISHED</p>
                </div>
                <div>
                  <p className="text-xs text-gray-500">Brier OOF</p>
                  <p className="font-bold">{deployed_model.oof_brier_score.toFixed(4)}</p>
                  <p className="text-[10px] text-gray-600 mt-0.5">
                    {deployed_model.trained_at
                      ? new Date(deployed_model.trained_at).toLocaleDateString("fr-FR")
                      : "—"}
                  </p>
                </div>
              </div>
            </div>
          )}

          {/* KPIs globaux */}
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
            <Kpi
              label="Prédictions évaluées"
              value={overall.n.toString()}
              sub={`sur ${overall.window_days} jours`}
            />
            <Kpi
              label="Accuracy global"
              value={`${((overall.accuracy ?? 0) * 100).toFixed(1)}%`}
              sub="argmax = résultat réel"
              accent={(overall.accuracy ?? 0) >= 0.45 ? "good" : "bad"}
            />
            <Kpi
              label="Log-loss"
              value={(overall.log_loss ?? 0).toFixed(4)}
              sub="plus bas = meilleur"
              accent={(overall.log_loss ?? 99) < 1.1 ? "good" : "bad"}
            />
            <Kpi
              label="Brier score"
              value={(overall.brier_score ?? 0).toFixed(4)}
              sub="calibration probabilités"
            />
          </div>

          {/* Per-class accuracy */}
          <div className="bg-gray-900 border border-gray-800 rounded-xl p-5">
            <h2 className="text-sm font-semibold mb-4 text-gray-300">
              Précision par type de résultat
            </h2>
            <div className="grid grid-cols-3 gap-4">
              <ClassAccuracy
                label="Domicile"
                accuracy={overall.home_accuracy}
                share={overall.outcome_distribution?.home_pct}
                color="text-brand-400"
              />
              <ClassAccuracy
                label="Nul"
                accuracy={overall.draw_accuracy}
                share={overall.outcome_distribution?.draw_pct}
                color="text-yellow-400"
              />
              <ClassAccuracy
                label="Extérieur"
                accuracy={overall.away_accuracy}
                share={overall.outcome_distribution?.away_pct}
                color="text-purple-400"
              />
            </div>
          </div>

          {/* Daily timeline */}
          {daily.length >= 2 && (
            <div className="bg-gray-900 border border-gray-800 rounded-xl p-5">
              <h2 className="text-sm font-semibold mb-4 text-gray-300 flex items-center gap-2">
                <Calendar className="w-4 h-4" />
                Évolution quotidienne
              </h2>
              <div className="h-64">
                <ResponsiveContainer width="100%" height="100%">
                  <LineChart data={daily}>
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
                      yAxisId="acc"
                      stroke="#6b7280"
                      tick={{ fontSize: 11 }}
                      domain={[0, 1]}
                      tickFormatter={(v) => `${(v * 100).toFixed(0)}%`}
                    />
                    <YAxis
                      yAxisId="ll"
                      orientation="right"
                      stroke="#6b7280"
                      tick={{ fontSize: 11 }}
                      domain={[0.5, 1.5]}
                    />
                    <Tooltip
                      contentStyle={{ backgroundColor: "#111827", border: "1px solid #1f2937", borderRadius: 8 }}
                      labelFormatter={(d) => new Date(d).toLocaleDateString("fr-FR")}
                      formatter={(val: number, name: string) =>
                        name === "Accuracy" ? `${(val * 100).toFixed(1)}%` : val.toFixed(4)
                      }
                    />
                    <Legend wrapperStyle={{ fontSize: 12 }} />
                    <Line
                      yAxisId="acc"
                      type="monotone"
                      dataKey="accuracy"
                      stroke="#22d3ee"
                      strokeWidth={2}
                      dot={{ r: 3 }}
                      name="Accuracy"
                    />
                    <Line
                      yAxisId="ll"
                      type="monotone"
                      dataKey="log_loss"
                      stroke="#f59e0b"
                      strokeWidth={2}
                      dot={{ r: 3 }}
                      name="Log-loss"
                    />
                  </LineChart>
                </ResponsiveContainer>
              </div>
            </div>
          )}

          {/* Par version */}
          {by_version.length > 0 && (
            <div className="bg-gray-900 border border-gray-800 rounded-xl p-5">
              <h2 className="text-sm font-semibold mb-4 text-gray-300">
                Performance par version de modèle
              </h2>
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="text-left text-xs text-gray-500 border-b border-gray-800">
                      <th className="py-2 pr-3">Version</th>
                      <th className="py-2 px-3 text-right">N</th>
                      <th className="py-2 px-3 text-right">Accuracy</th>
                      <th className="py-2 px-3 text-right">Log-loss</th>
                      <th className="py-2 px-3 text-right">Brier</th>
                    </tr>
                  </thead>
                  <tbody>
                    {by_version.map((v) => (
                      <tr key={v.model_version} className="border-b border-gray-800/50">
                        <td className="py-2 pr-3 font-mono text-xs">{v.model_version}</td>
                        <td className="py-2 px-3 text-right text-gray-400">{v.n}</td>
                        <td className="py-2 px-3 text-right font-semibold">
                          {v.accuracy !== null ? `${(v.accuracy * 100).toFixed(1)}%` : "—"}
                        </td>
                        <td className="py-2 px-3 text-right">
                          {v.log_loss !== null ? v.log_loss.toFixed(4) : "—"}
                        </td>
                        <td className="py-2 px-3 text-right text-gray-400">
                          {v.brier_score !== null ? v.brier_score.toFixed(4) : "—"}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}

function ClassAccuracy({ label, accuracy, share, color }: {
  label: string;
  accuracy: number | null;
  share?: number;
  color: string;
}) {
  return (
    <div>
      <p className="text-xs text-gray-500 mb-1">{label}</p>
      <p className={cn("text-xl font-bold", color)}>
        {accuracy !== null ? `${(accuracy * 100).toFixed(1)}%` : "—"}
      </p>
      {share !== undefined && (
        <p className="text-xs text-gray-600 mt-1">
          {share.toFixed(1)}% des matchs
        </p>
      )}
    </div>
  );
}
