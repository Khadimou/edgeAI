"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { authApi } from "@/lib/api";
import { useAuthStore } from "@/store/auth";
import { cn } from "@/lib/utils";
import { Wallet, Shield, Target, ChevronRight, Check } from "lucide-react";

const STEPS = [
  { id: 1, label: "Bankroll", icon: Wallet },
  { id: 2, label: "Stratégie", icon: Shield },
  { id: 3, label: "Profil", icon: Target },
];

type RiskProfileValue = "CONSERVATIVE" | "MODERATE" | "AGGRESSIVE";

const RISK_OPTIONS: Array<{ value: RiskProfileValue; label: string; desc: string }> = [
  { value: "CONSERVATIVE", label: "Conservateur", desc: "Mises très faibles · Risque minimal · Idéal pour débuter" },
  { value: "MODERATE", label: "Modéré", desc: "Équilibre optimal risque/rendement · Recommandé" },
  { value: "AGGRESSIVE", label: "Agressif", desc: "Mises plus élevées · Croissance plus rapide, variance plus haute" },
];

export default function OnboardingPage() {
  const router = useRouter();
  const { updateUser } = useAuthStore();
  const [step, setStep] = useState(1);
  const [bankroll, setBankroll] = useState("500");
  const [riskProfile, setRiskProfile] = useState<RiskProfileValue>("MODERATE");
  const [kellyFraction, setKellyFraction] = useState(0.5);
  const [saving, setSaving] = useState(false);

  async function finish() {
    setSaving(true);
    try {
      await authApi.updateProfile({
        bankroll: parseFloat(bankroll),
        risk_profile: riskProfile,
        kelly_fraction: kellyFraction,
      });
      updateUser({
        bankroll: parseFloat(bankroll),
        riskProfile: riskProfile as "CONSERVATIVE" | "MODERATE" | "AGGRESSIVE",
        kellyFraction,
      });
      router.push("/dashboard");
    } catch {
      router.push("/dashboard");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="min-h-screen flex items-center justify-center p-4">
      <div className="w-full max-w-lg">
        {/* Logo */}
        <div className="text-center mb-8">
          <span className="text-2xl font-bold text-brand-500">edgeAI</span>
          <p className="text-gray-400 mt-1">Configurons votre compte en 3 étapes</p>
        </div>

        {/* Progress */}
        <div className="flex items-center justify-center gap-3 mb-10">
          {STEPS.map((s, i) => (
            <div key={s.id} className="flex items-center gap-3">
              <div className={cn(
                "flex items-center gap-2 px-3 py-1.5 rounded-full text-sm font-medium transition-all",
                step === s.id ? "bg-brand-600 text-white" :
                step > s.id ? "bg-edge-green/20 text-edge-green" : "bg-gray-800 text-gray-500"
              )}>
                {step > s.id ? <Check className="w-3.5 h-3.5" /> : <s.icon className="w-3.5 h-3.5" />}
                {s.label}
              </div>
              {i < STEPS.length - 1 && <div className={cn("h-px w-8", step > s.id + 0 ? "bg-edge-green" : "bg-gray-800")} />}
            </div>
          ))}
        </div>

        {/* Step 1 — Bankroll */}
        {step === 1 && (
          <div className="card space-y-5">
            <div>
              <h2 className="text-xl font-bold mb-1">Quelle est votre bankroll ?</h2>
              <p className="text-gray-400 text-sm">
                La bankroll est le capital total que vous allouez aux paris. edgeAI calculera
                les mises en proportion de ce montant. Vous pouvez la modifier à tout moment.
              </p>
            </div>
            <div>
              <label className="block text-sm font-medium mb-1.5">Bankroll totale (€)</label>
              <input
                type="number"
                className="input text-lg"
                value={bankroll}
                onChange={(e) => setBankroll(e.target.value)}
                min="50"
                step="50"
                placeholder="500"
              />
              <div className="flex gap-2 mt-2">
                {["200", "500", "1000", "2000"].map((v) => (
                  <button
                    key={v}
                    onClick={() => setBankroll(v)}
                    className={cn(
                      "px-3 py-1 rounded text-sm transition-colors",
                      bankroll === v ? "bg-brand-600 text-white" : "bg-gray-800 text-gray-400 hover:bg-gray-700"
                    )}
                  >
                    {v}€
                  </button>
                ))}
              </div>
            </div>
            <div className="p-3 rounded-lg bg-blue-500/10 border border-blue-500/20 text-sm text-blue-300">
              💡 Conseil : Commencez avec un montant que vous pouvez vous permettre de perdre entièrement.
              L&apos;edge se matérialise sur le long terme.
            </div>
            <button onClick={() => setStep(2)} className="btn-primary w-full" disabled={!bankroll || parseFloat(bankroll) < 50}>
              Continuer <ChevronRight className="w-4 h-4 ml-1 inline" />
            </button>
          </div>
        )}

        {/* Step 2 — Stratégie */}
        {step === 2 && (
          <div className="card space-y-5">
            <div>
              <h2 className="text-xl font-bold mb-1">Votre profil de risque</h2>
              <p className="text-gray-400 text-sm">
                Détermine quelle fraction du Kelly plein sera utilisée pour calculer vos mises.
              </p>
            </div>
            <div className="space-y-2">
              {RISK_OPTIONS.map((opt) => (
                <label
                  key={opt.value}
                  className={cn(
                    "flex items-start gap-3 p-4 rounded-lg border cursor-pointer transition-colors",
                    riskProfile === opt.value ? "border-brand-500 bg-brand-500/5" : "border-gray-800 hover:border-gray-700"
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
                    <p className="font-medium">{opt.label}</p>
                    <p className="text-sm text-gray-400">{opt.desc}</p>
                  </div>
                </label>
              ))}
            </div>
            <div className="flex gap-3">
              <button onClick={() => setStep(1)} className="btn-secondary flex-1">Retour</button>
              <button onClick={() => setStep(3)} className="btn-primary flex-1">Continuer</button>
            </div>
          </div>
        )}

        {/* Step 3 — Résumé */}
        {step === 3 && (
          <div className="card space-y-5">
            <div>
              <h2 className="text-xl font-bold mb-1">Prêt à commencer !</h2>
              <p className="text-gray-400 text-sm">Voici votre configuration :</p>
            </div>
            <div className="space-y-3">
              {[
                { label: "Bankroll", value: `${parseFloat(bankroll).toFixed(0)}€` },
                { label: "Profil de risque", value: RISK_OPTIONS.find((o) => o.value === riskProfile)?.label || riskProfile },
                { label: "Fraction Kelly", value: `${(kellyFraction * 100).toFixed(0)}%` },
                { label: "Mise max par pari", value: `${(parseFloat(bankroll) * 0.10).toFixed(0)}€ (cap 10%)` },
              ].map((item) => (
                <div key={item.label} className="flex justify-between py-2.5 border-b border-gray-800 last:border-0">
                  <span className="text-gray-400 text-sm">{item.label}</span>
                  <span className="font-medium text-sm">{item.value}</span>
                </div>
              ))}
            </div>
            <div className="p-3 rounded-lg bg-yellow-500/10 border border-yellow-500/20 text-xs text-yellow-400">
              ⚠️ Jeu responsable : Les paris comportent un risque de perte. edgeAI fournit des conseils
              analytiques, pas des garanties. +18 uniquement.
            </div>
            <div className="flex gap-3">
              <button onClick={() => setStep(2)} className="btn-secondary flex-1">Retour</button>
              <button onClick={finish} disabled={saving} className="btn-primary flex-1">
                {saving ? "Sauvegarde..." : "Accéder au dashboard"}
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
