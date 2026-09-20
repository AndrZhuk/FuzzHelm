import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'

/** ExplainView — кульмінація захисту, тому вона і є стартовим маршрутом. */
const routes: RouteRecordRaw[] = [
  { path: '/', redirect: '/explain' },
  {
    path: '/explain/:id?',
    name: 'explain',
    component: () => import('./views/ExplainView.vue'),
    props: true,
  },
  { path: '/live', name: 'live', component: () => import('./views/LiveView.vue') },
  { path: '/backtest', name: 'backtest', component: () => import('./views/BacktestView.vue') },
  { path: '/:pathMatch(.*)*', redirect: '/explain' },
]

export const router = createRouter({
  history: createWebHistory(),
  routes,
  scrollBehavior: (_to, _from, saved) => saved ?? { top: 0 },
})
