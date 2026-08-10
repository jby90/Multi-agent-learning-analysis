declare module 'virtual:knowledge-catalog' {
  const catalog: import('./types/trace').KnowledgeCatalogEntry[]
  export default catalog
}

declare module 'virtual:profile-catalog' {
  const profiles: import('./types/profile').LearnerProfileOption[]
  export default profiles
}

declare module 'virtual:diagnostic-experience-tags' {
  const tags: import('./types/diagnostic').DiagnosticExperienceTag[]
  export default tags
}
