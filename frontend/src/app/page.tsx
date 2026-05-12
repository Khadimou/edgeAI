import Link from "next/link";
import { TrendingUp, Shield, BarChart3, Brain, ChevronRight, Check } from "lucide-react";

const PLANS = [
  {
    name: "Gratuit",
    price: "0€",
    period: "",
    features: ["3 matchs analysés/semaine", "Aperçu des probabilités", "Historique 7 jours"],
    cta: "Commencer gratuitement",
    href: "/register",
    highlight: false,
  },
  {
    name: "Pro",
    price: "19€",
    period: "/mois",
    features: [
      "Matchs illimités",
      "Recommandations Kelly complètes",
      "Alertes opportunités",
      "Historique illimité",
      "Football + Tennis",
    ],
    cta: "Essai 14 jours gratuit",
    href: "/register?plan=pro",
    highlight: true,
  },
  {
    name: "Elite",
    price: "49€",
    period: "/mois",
    features: [
      "Tout Pro inclus",
      "Alertes Push + SMS",
      "Tous les sports",
      "Accès API (rate limited)",
      "Support chat dédié",
    ],
    cta: "Commencer Elite",
    href: "/register?plan=elite",
    highlight: false,
  },
];

const FEATURES = [
  {
    icon: Brain,
    title: "IA Prédictive",
    desc: "Modèle XGBoost entraîné sur 5 ligues majeures avec 40 variables de performance.",
  },
  {
    icon: BarChart3,
    title: "Critère de Kelly",
    desc: "Mise optimale calculée automatiquement selon votre bankroll et profil de risque.",
  },
  {
    icon: TrendingUp,
    title: "Value Betting",
    desc: "Détection automatique des paris à espérance positive (edge > 3%).",
  },
  {
    icon: Shield,
    title: "Gestion du Risque",
    desc: "Stop-loss mensuel, cap par pari et limite de paris simultanés intégrés.",
  },
];

export default function LandingPage() {
  return (
    <div className="min-h-screen bg-gray-950">
      {/* Nav */}
      <nav className="border-b border-gray-800 px-6 py-4">
        <div className="max-w-6xl mx-auto flex items-center justify-between">
          <span className="text-xl font-bold text-brand-500">edgeAI</span>
          <div className="flex items-center gap-4">
            <Link href="/login" className="text-sm text-gray-400 hover:text-gray-100 transition-colors">
              Connexion
            </Link>
            <Link href="/register" className="btn-primary">
              Commencer gratuitement
            </Link>
          </div>
        </div>
      </nav>

      {/* Hero */}
      <section className="max-w-6xl mx-auto px-6 py-24 text-center">
        <div className="inline-flex items-center gap-2 px-3 py-1 rounded-full bg-brand-500/10 border border-brand-500/20 text-brand-400 text-sm mb-8">
          <span className="w-2 h-2 rounded-full bg-brand-500 animate-pulse" />
          Bêta ouverte — 200 premiers utilisateurs
        </div>
        <h1 className="text-5xl md:text-6xl font-bold mb-6 leading-tight">
          Pariez avec{" "}
          <span className="text-transparent bg-clip-text bg-gradient-to-r from-brand-400 to-brand-600">
            l&apos;avantage mathématique
          </span>
        </h1>
        <p className="text-xl text-gray-400 max-w-2xl mx-auto mb-10">
          edgeAI ne promet pas de gagner à chaque pari. On vous donne les outils pour être{" "}
          <strong className="text-gray-200">gagnant sur le long terme</strong> grâce au value
          betting et au critère de Kelly.
        </p>
        <div className="flex flex-col sm:flex-row gap-4 justify-center">
          <Link href="/register" className="btn-primary text-base px-6 py-3">
            Commencer gratuitement
            <ChevronRight className="w-4 h-4 ml-1" />
          </Link>
          <Link href="#how" className="btn-secondary text-base px-6 py-3">
            Comment ça marche ?
          </Link>
        </div>

        {/* Stats */}
        <div className="mt-20 grid grid-cols-3 gap-8 max-w-lg mx-auto">
          {[
            { label: "Ligues couvertes", value: "5" },
            { label: "Variables ML", value: "40" },
            { label: "Précision modèle", value: ">54%" },
          ].map((s) => (
            <div key={s.label}>
              <div className="text-3xl font-bold text-brand-400">{s.value}</div>
              <div className="text-sm text-gray-500 mt-1">{s.label}</div>
            </div>
          ))}
        </div>
      </section>

      {/* Features */}
      <section id="how" className="max-w-6xl mx-auto px-6 py-20">
        <h2 className="text-3xl font-bold text-center mb-4">Comment edgeAI fonctionne</h2>
        <p className="text-gray-400 text-center mb-14 max-w-xl mx-auto">
          Un système mathématique complet — de la prédiction IA à la mise optimale.
        </p>
        <div className="grid md:grid-cols-2 lg:grid-cols-4 gap-6">
          {FEATURES.map((f) => (
            <div key={f.title} className="card hover:border-gray-700 transition-colors">
              <f.icon className="w-8 h-8 text-brand-500 mb-4" />
              <h3 className="font-semibold text-lg mb-2">{f.title}</h3>
              <p className="text-sm text-gray-400">{f.desc}</p>
            </div>
          ))}
        </div>
      </section>

      {/* Pricing */}
      <section className="max-w-6xl mx-auto px-6 py-20">
        <h2 className="text-3xl font-bold text-center mb-4">Tarification transparente</h2>
        <p className="text-gray-400 text-center mb-14">
          Commencez gratuitement. Upgradez quand vous êtes convaincu.
        </p>
        <div className="grid md:grid-cols-3 gap-6">
          {PLANS.map((plan) => (
            <div
              key={plan.name}
              className={`card flex flex-col ${
                plan.highlight
                  ? "border-brand-500 ring-1 ring-brand-500 relative"
                  : ""
              }`}
            >
              {plan.highlight && (
                <div className="absolute -top-3 left-1/2 -translate-x-1/2 px-3 py-1 bg-brand-600 text-white text-xs rounded-full font-semibold">
                  Populaire
                </div>
              )}
              <div className="mb-6">
                <h3 className="font-bold text-xl mb-2">{plan.name}</h3>
                <div className="flex items-baseline gap-1">
                  <span className="text-4xl font-bold">{plan.price}</span>
                  <span className="text-gray-400">{plan.period}</span>
                </div>
              </div>
              <ul className="flex-1 space-y-3 mb-6">
                {plan.features.map((f) => (
                  <li key={f} className="flex items-center gap-2 text-sm">
                    <Check className="w-4 h-4 text-edge-green flex-shrink-0" />
                    {f}
                  </li>
                ))}
              </ul>
              <Link
                href={plan.href}
                className={plan.highlight ? "btn-primary" : "btn-secondary"}
              >
                {plan.cta}
              </Link>
            </div>
          ))}
        </div>
      </section>

      {/* Disclaimer */}
      <section className="max-w-4xl mx-auto px-6 py-12 text-center">
        <div className="card border-yellow-500/20 bg-yellow-500/5">
          <p className="text-sm text-yellow-400/80">
            <strong className="text-yellow-400">Jeu responsable :</strong> Les paris sportifs
            comportent des risques de perte. edgeAI est un outil d&apos;analyse, pas un opérateur de
            paris. Vous devez avoir 18 ans ou plus. En cas de problème :{" "}
            <a href="https://www.joueurs-info-service.fr" className="underline" target="_blank" rel="noreferrer">
              Joueurs Info Service 09 74 75 13 13
            </a>
          </p>
        </div>
      </section>

      <footer className="border-t border-gray-800 py-8 text-center text-sm text-gray-500">
        © 2026 edgeAI · Tous droits réservés ·{" "}
        <Link href="/legal" className="hover:text-gray-300">Mentions légales</Link>
      </footer>
    </div>
  );
}
