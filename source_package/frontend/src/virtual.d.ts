declare module 'virtual:knowledge-catalog' {
  const catalog: import('./types/trace').KnowledgeCatalogEntry[]
  export default catalog
}

declare module 'virtual:profile-catalog' {
  const profiles: import('./types/profile').LearnerProfileOption[]
  export default profiles
}
