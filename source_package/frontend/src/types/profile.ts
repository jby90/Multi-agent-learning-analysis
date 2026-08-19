export interface LearnerProfileOption {
  id: string
  title: string
  background: string
  strengths: string[]
  /** 画像学习领域内的知识点清单（闭环一：训练关注点按域过滤） */
  knowledgeScope: string[]
  /** 实操模式：sql=学员书写查询；data_present=系统代执行并呈现（闭环五） */
  practiceMode: string
}
