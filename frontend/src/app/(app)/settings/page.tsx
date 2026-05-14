"use client";

import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { authApi } from "@/lib/api";
import { useAuthStore } from "@/store/auth";
import { cn } from "@/lib/utils";
import { Check, Crown } from "lucide-react";

type RiskProfileValue = "CONSERVATIVE" | "MODERATE" | "AGGRESSIVE";

const RISK_OPTIONS: Array<{ value: RiskProfileValue; label: string; desc: string; color: string }> = [
  {
    value: "CONSERVATIVE",
    label: "Conservateur",
    desc: "Kelly × 25% — Mise très faible, risque minimal",
    color: "border-blue-500/40 bg-blue-500/5",
  },
  {
    value: "MODERATE",
    label: "Modéré",
    desc: "Kelly × 50% — Équilibre risque/rendement",
    color: "border-brand-500/40 bg-brand-500/5",
  },
  {
    value: "AGGRESSIVE",
    label: "Agressif",
    desc: "Kelly × 75% — Mise élevée, croissance maximale",
    color: "border-orange-500/40 bg-orange-500/5",
  },
];

const PLANS = [
  { name: "Pro", price: "19€/mois", features: ["Recommandations Kelly", "Matchs illimités", "Alertes"], highlight: false },
  { name: "Elite", price: "49€/mois", features: ["Tout Pro", "Push + SMS", "API access"], highlight: true },
];

export default function SettingsPage() {
  const { user, updateUser } = useAuthStore();
  const qc = useQueryClient();

  const [bankroll, setBankroll] = useState(user?.bankroll.toString() ?? "");
  const [riskProfile, setRiskProfile] = useState<RiskProfileValue>((user?.risk_profile as RiskProfileValue) ?? "MODERATE");
  const [kellyFraction, setKellyFraction] = useState(user?.kelly_fraction || 0.5);
  const [alertsEnabled, setAlertsEnabled] = useState(user?.alerts_enabled ?? true);
  const [maxBets, setMaxBets] = useState(user?.max_bets_per_day || 3);
  const [goalAmount, setGoalAmount] = useState(user?.goal_amount?.toString() ?? "");
  const [goalDays, setGoalDays] = useState(user?.goal_timeframe_days?.toString() ?? "30");
  const [saved, setSaved] = useState(false);

  const { mutate: save, isPending } = useMutation({
    mutationFn: () =>
      authApi.updateProfile({
        bankroll: parseFloat(bankroll),
        risk_profile: riskProfile,
        kelly_fraction: kellyFraction,
        alerts_enabled: alertsEnabled,
        max_bets_per_day: maxBets,
        goal_amount: goalAmount ? parseFloat(goalAmount) : null,
        goal_timeframe_days: goalDays ? parseInt(goalDays) : null,
        goal_start_date: (goalAmount && !user?.goal_amount) ? new Date().toISOString() : undefined,
      }),
    onSuccess: ({ data }) => {
      updateUser({
        bankroll: data.bankroll,
        risk_profile: data.risk_profile,
        kelly_fraction: data.kelly_fraction,
        alerts_enabled: data.alerts_enabled,
        max_bets_per_day: data.max_bets_per_day,
        goal_amount: data.goal_amount,
        goal_timeframe_days: data.goal_timeframe_days,
        goal_start_date: data.goal_start_date,
      });
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
      qc.invalidateQueries({ queryKey: ["bankroll"] });
    },
  });

  return (
    <div className="space-y-8 max-w-2xl">
      <h1 className="text-2xl font-bold">Paramètres</h1>

      {/* Plan actuel */}
      <div className="card">
        <div className="flex items-center justify-between mb-4">
          <h2 className="font-semibold">Abonnement</h2>
          <span className={cn(
            "text-xs px-2.5 py-1 rounded-full font-semibold",
            user?.plan === "ELITE" ? "bg-purple-500/20 text-purple-400" :
            user?.plan === "PRO" ? "bg-brand-500/20 text-brand-400" :
            "bg-gray-700 text-gray-400"
          )}>
            Plan {user?.plan}
          </span>
        </div>
        {user?.plan === "FREE" && (
          <div className="grid grid-cols-2 gap-3">
            {PLANS.map((plan) => (
              <div key={plan.name} className={cn("card p-4", plan.highlight ? "border-brand-500/40" : "")}>
                <div className="flex items-center gap-2 mb-2">
                  {plan.highlight && <Crown className="w-4 h-4 text-brand-400" />}
                  <span className="font-semibold">{plan.name}</span>
                  <span className="text-gray-400 text-sm ml-auto">{plan.price}</span>
                </div>
                <ul className="space-y-1 mb-3">
                  {plan.features.map((f) => (
                    <li key={f} className="text-xs text-gray-400 flex items-center gap-1">
                      <Check className="w-3 h-3 text-edge-green" />{f}
                    </li>
                  ))}
                </ul>
                <button className={plan.highlight ? "btn-primary w-full text-xs py-1.5" : "btn-secondary w-full text-xs py-1.5"}>
                  Passer à {plan.name}
                </button>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Objectif */}
      <div className="card">
        <h2 className="font-semibold mb-1">Mon objectif</h2>
        <p className="text-xs text-gray-400 mb-4">Définissez un gain cible et un horizon pour recevoir un plan de mise personnalisé.</p>
        <div className="grid grid-cols-2 gap-4">
          <div>
            <label className="block text-sm font-medium mb-1.5">Gain visé (€)</label>
            <input
              type="number"
              className="input w-full"
              placeholder="ex : 500"
              value={goalAmount}
              onChange={(e) => setGoalAmount(e.target.value)}
              min="1"
              step="50"
            />
          </div>
          <div>
            <label className="block text-sm font-medium mb-1.5">Horizon (jours)</label>
            <select
              className="input w-full"
              value={goalDays}
              onChange={(e) => setGoalDays(e.target.value)}
            >
              <option value="7">1 semaine</option>
              <option value="14">2 semaines</option>
              <option value="30">1 mois</option>
              <option value="60">2 mois</option>
              <option value="90">3 mois</option>
            </select>
          </div>
        </div>
        {goalAmount && parseFloat(goalAmount) > 0 && bankroll && parseFloat(bankroll) > 0 && (
          <p className="text-xs text-brand-400 mt-3">
            ROI nécessaire : +{((parseFloat(goalAmount) / parseFloat(bankroll)) * 100).toFixed(1)}% sur {goalDays} jours
          </p>
        )}
      </div>

      {/* Bankroll */}
      <div className="card">
        <h2 className="font-semibold mb-4">Bankroll & Stratégie</h2>
        <div className="space-y-5">
          <div>
            <label className="block text-sm font-medium mb-1.5">
              Bankroll totale (€)
            </label>
            <div className="flex items-center gap-3">
              <input
                type="number"
                className="input w-36"
                value={bankroll}
                onChange={(e) => setBankroll(e.target.value)}
                min="0"
                step="50"
                placeholder="0"
              />
              <div className="flex gap-2">
                {[100, 250, 500, 1000, 2000].map((v) => (
                  <button
                    key={v}
                    type="button"
                    onClick={() => setBankroll(v.toString())}
                    className={cn(
                      "px-2.5 py-1 rounded text-xs font-medium border transition-colors",
                      bankroll === v.toString()
                        ? "border-brand-500 bg-brand-500/20 text-brand-400"
                        : "border-gray-700 text-gray-400 hover:border-gray-600"
                    )}
                  >
                    {v}€
                  </button>
                ))}
              </div>
            </div>
            <p className="text-xs text-gray-500 mt-1">
              Montant total alloué aux paris. Utilisé pour calculer les mises Kelly.
            </p>
          </div>

          <div>
            <label className="block text-sm font-medium mb-3">Profil de risque</label>
            <div className="space-y-2">
              {RISK_OPTIONS.map((opt) => (
                <label
                  key={opt.value}
                  className={cn(
                    "flex items-start gap-3 p-3 rounded-lg border cursor-pointer transition-colors",
                    riskProfile === opt.value ? opt.color : "border-gray-800 hover:border-gray-700"
                  )}
                >
                  <input
                    type="radio"
                    name="risk"
                    value={opt.value}
                    checked={riskProfile === opt.value}
                    onChange={() => setRiskProfile(opt.value)}
                    className="mt-0.5 accent-brand-500"
                  />
                  <div>
                    <p className="text-sm font-medium">{opt.label}</p>
                    <p className="text-xs text-gray-400">{opt.desc}</p>
                  </div>
                </label>
              ))}
            </div>
          </div>

          <div>
            <label className="block text-sm font-medium mb-1.5">
              Fraction Kelly personnalisée : <span className="text-brand-400">{(kellyFraction * 100).toFixed(0)}%</span>
            </label>
            <input
              type="range"
              min="0.1"
              max="1"
              step="0.05"
              value={kellyFraction}
              onChange={(e) => setKellyFraction(parseFloat(e.target.value))}
              className="w-full accent-brand-500"
            />
            <div className="flex justify-between text-xs text-gray-500 mt-1">
              <span>Très prudent (10%)</span>
              <span>Kelly plein (100%)</span>
            </div>
          </div>

          <div>
            <label className="block text-sm font-medium mb-1.5">
              Maximum de paris simultanés : <span className="text-brand-400">{maxBets}</span>
            </label>
            <input
              type="range"
              min="1"
              max="10"
              step="1"
              value={maxBets}
              onChange={(e) => setMaxBets(parseInt(e.target.value))}
              className="w-full accent-brand-500"
            />
          </div>

          <div className="flex items-center justify-between">
            <div>
              <p className="text-sm font-medium">Alertes opportunités</p>
              <p className="text-xs text-gray-400">Notifier quand edge {">"} 5%</p>
            </div>
            <button
              onClick={() => setAlertsEnabled(!alertsEnabled)}
              className={cn(
                "relative inline-flex h-6 w-11 rounded-full transition-colors",
                alertsEnabled ? "bg-brand-600" : "bg-gray-700"
              )}
            >
              <span className={cn(
                "inline-block h-5 w-5 rounded-full bg-white shadow-sm transition-transform mt-0.5",
                alertsEnabled ? "translate-x-5.5 ml-0.5" : "translate-x-0.5"
              )} />
            </button>
          </div>
        </div>
      </div>

      <div className="flex items-center gap-3">
        <button
          onClick={() => save()}
          disabled={isPending}
          className="btn-primary px-6"
        >
          {isPending ? "Sauvegarde..." : "Sauvegarder"}
        </button>
        {saved && <span className="text-sm text-edge-green flex items-center gap-1"><Check className="w-4 h-4" /> Sauvegardé</span>}
      </div>

      {/* Compte */}
      <div className="card border-gray-800">
        <h2 className="font-semibold mb-3">Compte</h2>
        <div className="text-sm text-gray-400 space-y-2">
          <p>Email : <span className="text-gray-200">{user?.email}</span></p>
          <p>Code parrainage : <span className="font-mono text-brand-400">{user?.referral_code || "—"}</span></p>
          <p className="text-xs mt-2">
            Vos données sont exportables et supprimables conformément au RGPD (Art. 17 & 20).{" "}
            <a href="mailto:privacy@edgeai.fr" className="text-brand-400 hover:underline">Contacter le DPO</a>
          </p>
        </div>
      </div>
    </div>
  );
}
