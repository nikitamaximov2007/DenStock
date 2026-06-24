/** @type {import('tailwindcss').Config} */
module.exports = {
  // Сканируем шаблоны Django для классов утилит (используется на UI-слоях).
  content: ["../templates/**/*.html", "../apps/**/templates/**/*.html"],
  theme: {
    extend: {},
  },
  plugins: [],
};
