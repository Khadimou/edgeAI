"use client";

import { Suspense, useState } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { authApi } from "@/lib/api";
import { useAuthStore } from "@/store/auth";

function RegisterForm() {
  const router = useRouter();
  const params = useSearchParams();
  const plan = params.get("plan");
  const setAuth = useAuthStore((s) => s.setAuth);

  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [accepted, setAccepted] = useState(false);
  const [ageVerified, setAgeVerified] = useState(false);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!accepted || !ageVerified) {
      setError("Vous devez accepter les conditions et confirmer votre âge.");
      return;
    }
    setError("");
    setLoading(true);
    try {
      const { data: token } = await authApi.register(email, password, name);
      const { data: user } = await authApi.me();
      setAuth(user, token.access_token, token.refresh_token);
      router.push("/onboarding");
    } catch (err: unknown) {
      const msg = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      setError(msg ?? "Erreur lors de l'inscription");
    } finally {
      setLoading(false);
    }
  }

  return (
    <>
      <div className="text-center mb-8">
        <Link href="/" className="text-2xl font-bold text-brand-500">edgeAI</Link>
        <p className="text-gray-400 mt-2">
          {plan === "pro" ? "Essai Pro gratuit 14 jours" : "Créer votre compte gratuit"}
        </p>
      </div>
      <form onSubmit={handleSubmit} className="card space-y-4">
        {error && (
          <div className="p-3 rounded-lg bg-red-500/10 border border-red-500/20 text-red-400 text-sm">
            {error}
          </div>
        )}
        <div>
          <label className="block text-sm font-medium mb-1.5">Prénom (optionnel)</label>
          <input
            type="text"
            className="input"
            placeholder="Alex"
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
        </div>
        <div>
          <label className="block text-sm font-medium mb-1.5">Email</label>
          <input
            type="email"
            className="input"
            placeholder="vous@exemple.fr"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            required
          />
        </div>
        <div>
          <label className="block text-sm font-medium mb-1.5">Mot de passe</label>
          <input
            type="password"
            className="input"
            placeholder="Minimum 8 caractères"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            minLength={8}
            required
          />
        </div>
        <div className="space-y-2 text-sm">
          <label className="flex items-start gap-2 cursor-pointer">
            <input
              type="checkbox"
              checked={ageVerified}
              onChange={(e) => setAgeVerified(e.target.checked)}
              className="mt-0.5 accent-brand-500"
            />
            <span className="text-gray-400">
              Je confirme avoir <strong className="text-gray-200">18 ans ou plus</strong>
            </span>
          </label>
          <label className="flex items-start gap-2 cursor-pointer">
            <input
              type="checkbox"
              checked={accepted}
              onChange={(e) => setAccepted(e.target.checked)}
              className="mt-0.5 accent-brand-500"
            />
            <span className="text-gray-400">
              J&apos;accepte les{" "}
              <Link href="/legal" className="text-brand-400 hover:underline">conditions d&apos;utilisation</Link>
              {" "}et comprends que les paris sportifs comportent des risques de perte.
            </span>
          </label>
        </div>
        <button type="submit" className="btn-primary w-full" disabled={loading}>
          {loading ? "Création..." : "Créer mon compte"}
        </button>
        <p className="text-center text-sm text-gray-400">
          Déjà un compte ?{" "}
          <Link href="/login" className="text-brand-400 hover:underline">Se connecter</Link>
        </p>
      </form>
    </>
  );
}

export default function RegisterPage() {
  return (
    <div className="min-h-screen flex items-center justify-center p-4">
      <div className="w-full max-w-sm">
        <Suspense fallback={
          <div className="flex items-center justify-center h-40">
            <div className="animate-spin w-8 h-8 border-2 border-brand-500 border-t-transparent rounded-full" />
          </div>
        }>
          <RegisterForm />
        </Suspense>
      </div>
    </div>
  );
}
