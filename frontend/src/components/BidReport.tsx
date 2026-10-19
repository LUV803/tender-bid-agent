import type { BidDetail } from '../api'
import {
  formatDate,
  formatDuration,
  formatPercent,
  formatScore,
  roleLabel,
  severityClass,
  severityLabel,
} from '../format'
import { Icon } from '../icons'
import { asRecord, asText } from '../profile'
import { ArtifactActions } from './Artifacts'
import { Badge, Metric, SectionTitle } from './ui'

function CoverageBar({
  category,
  factors,
  covered,
  maxScore,
}: {
  category: string
  factors: number
  covered: number
  maxScore: number
}) {
  const ratio = factors ? covered / factors : 0
  return (
    <div className="coverage-row">
      <div className="coverage-row-head">
        <span>{category}</span>
        <small>
          {covered}/{factors} 项 · {formatScore(maxScore)} 分
        </small>
      </div>
      <div className="coverage-bar">
        <span style={{ width: `${Math.round(ratio * 100)}%` }} />
      </div>
    </div>
  )
}

export function BidReport({ detail }: { detail: BidDetail }) {
  const coverage = detail.coverage
  const stats = detail.stats
  const settings = asRecord(detail.settings)
  const issueCount = detail.issues.length
  const high = detail.issues.filter((issue) => issue.severity === 'high').length
  const medium = detail.issues.filter((issue) => issue.severity === 'medium').length
  const low = detail.issues.filter((issue) => issue.severity === 'low').length

  return (
    <section className="bid-report">
      <div className="report-head">
        <div className="report-heading">
          <div className="eyebrow">Bid Result / 编写结果</div>
          <h2>{detail.project_name || '未命名项目'}</h2>
          <p className="report-meta">
            <span>{detail.bidder || '投标人未填写'}</span>
            <span>生成于 {formatDate(detail.created_at)}</span>
            <span>用时 {formatDuration(detail.elapsed_seconds)}</span>
            <span>
              新增章节上限 {formatScore(settings.max_sections ?? 0)} · 修订上限{' '}
              {formatScore(settings.max_revisions ?? 0)}
            </span>
          </p>
        </div>
        <ArtifactActions artifacts={detail.artifacts} folder={detail.folder} />
      </div>

      {detail.warnings.length ? (
        <div className="warning-strip">
          <Icon name="alert" size={17} />
          <div>
            {detail.warnings.map((warning) => (
              <p key={warning}>{warning}</p>
            ))}
          </div>
        </div>
      ) : null}

      <div className="metric-grid">
        <Metric
          hint={
            coverage.total_factors
              ? `${coverage.covered_factors} / ${coverage.total_factors} 个评分项已映射到章节`
              : '未从招标文件中抽取到评分项'
          }
          icon="chart"
          label="评分项覆盖"
          value={formatPercent(coverage.factor_coverage, 0)}
        />
        <Metric
          hint={`${formatScore(coverage.covered_max_score)} / ${formatScore(
            coverage.max_score,
          )} 分`}
          icon="shield"
          label="分值覆盖"
          value={formatPercent(coverage.score_coverage, 0)}
        />
        <Metric
          hint={`自动修订 ${formatScore(stats.revisions ?? detail.revisions.length)} 个章节`}
          icon="document-check"
          label="章节"
          value={formatScore(stats.sections ?? detail.sections.length)}
        />
        <Metric
          hint={`${detail.profile_gap_count} 项投标人资料缺口`}
          icon="pen"
          label="待填写 / 待核对"
          tone={detail.pending_count ? 'warning' : 'neutral'}
          value={formatScore(detail.pending_count)}
        />
        <Metric
          hint={`高 ${high} · 中 ${medium} · 低 ${low}`}
          icon="alert"
          label="审查问题"
          tone={high ? 'danger' : medium ? 'warning' : 'success'}
          value={formatScore(issueCount)}
        />
      </div>

      <div className="report-grid">
        <section className="panel">
          <SectionTitle
            description="按评分项所属类别统计，仅表示评分项是否已映射到章节，不代表实际得分。"
            title="评分项覆盖"
          />
          {coverage.by_category.length ? (
            <div className="coverage-list">
              {coverage.by_category.map((item) => (
                <CoverageBar
                  category={item.category || '未分类'}
                  covered={item.covered}
                  factors={item.factors}
                  key={item.category}
                  maxScore={item.max_score}
                />
              ))}
            </div>
          ) : (
            <p className="muted">暂无评分项数据。</p>
          )}
          {coverage.unmapped_factors.length ? (
            <details className="report-details">
              <summary>
                <Icon name="alert" size={15} />
                未映射评分项（{coverage.unmapped_factors.length}）
              </summary>
              <ul className="compact-list">
                {coverage.unmapped_factors.map((item, index) => (
                  <li key={`${item.factor}-${index}`}>
                    <span>{item.factor || '未命名评分项'}</span>
                    <small>
                      {item.category || '未分类'} · {formatScore(item.max_score)} 分
                    </small>
                  </li>
                ))}
              </ul>
            </details>
          ) : null}
        </section>

        <section className="panel">
          <SectionTitle
            description="章节与评分项、审查问题的对应关系；分值上限来自招标文件评分办法。"
            title="章节得分覆盖"
          />
          <div className="table-scroll">
            <table className="data-table">
              <thead>
                <tr>
                  <th>章节</th>
                  <th>角色</th>
                  <th>评分项</th>
                  <th>分值上限</th>
                  <th>问题</th>
                </tr>
              </thead>
              <tbody>
                {detail.sections.map((section) => (
                  <tr key={section.id}>
                    <td>
                      <div className="cell-title">
                        {section.title}
                        {section.origin === 'analyst' ? (
                          <Badge tone="info">新增</Badge>
                        ) : null}
                      </div>
                      <small className="cell-sub">
                        目标 {formatScore(section.word_target)} 字
                      </small>
                    </td>
                    <td>
                      <Badge tone="neutral">{roleLabel(section.role)}</Badge>
                    </td>
                    <td>
                      <div className="cell-title">
                        {section.factor_count} 项
                      </div>
                      {section.factors.length ? (
                        <small className="cell-sub">
                          {section.factors.slice(0, 2).join('、')}
                          {section.factors.length > 2
                            ? ` 等 ${section.factors.length} 项`
                            : ''}
                        </small>
                      ) : (
                        <small className="cell-sub">未映射评分项</small>
                      )}
                    </td>
                    <td>{formatScore(section.factor_score)}</td>
                    <td>
                      {section.issue_count ? (
                        <span className="issue-count">
                          <span className="severity-dot danger" />
                          {section.issue_count}
                        </span>
                      ) : (
                        <span className="muted">—</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {!detail.sections.length ? (
            <p className="muted">清单中没有章节大纲。</p>
          ) : null}
        </section>
      </div>

      <section className="panel">
        <SectionTitle
          description="这是内部自检报告，不随投标文件提交。"
          title="审查意见"
          aside={
            <div className="issue-summary">
              <Badge tone="danger">高 {high}</Badge>
              <Badge tone="warning">中 {medium}</Badge>
              <Badge tone="neutral">低 {low}</Badge>
            </div>
          }
        />
        {detail.review_summary ? (
          <p className="review-summary">{detail.review_summary}</p>
        ) : null}
        {detail.issues.length ? (
          <ul className="issue-list">
            {detail.issues.map((issue, index) => (
              <li className="issue-item" key={`${issue.problem}-${index}`}>
                <span className={`severity-dot ${severityClass(issue.severity)}`} />
                <div className="issue-copy">
                  <div className="issue-head">
                    <Badge tone={severityClass(issue.severity) as 'danger' | 'warning' | 'info'}>
                      {severityLabel(issue.severity)}风险
                    </Badge>
                    <span className="issue-category">{issue.category || '审查'}</span>
                    {issue.section_id ? (
                      <code className="inline-code">{issue.section_id}</code>
                    ) : null}
                  </div>
                  <p>{issue.problem}</p>
                  {issue.fix ? (
                    <small>
                      <strong>建议：</strong>
                      {issue.fix}
                    </small>
                  ) : null}
                </div>
              </li>
            ))}
          </ul>
        ) : (
          <p className="muted">没有审查问题。</p>
        )}
      </section>

      <div className="report-grid report-grid-details">
        <details className="panel report-details" open={detail.pending_count > 0}>
          <summary>
            待填写 / 待核对（{detail.pending_count}）
            <Icon name="chevron-down" size={16} />
          </summary>
          {detail.pending_markers.length ? (
            <ul className="compact-list">
              {detail.pending_markers.map((item, index) => (
                <li key={`${item.marker}-${index}`}>
                  <span>{item.marker}</span>
                  {item.sections?.length ? (
                    <small>{item.sections.join('、')}</small>
                  ) : null}
                </li>
              ))}
            </ul>
          ) : (
            <p className="muted">没有占位符。</p>
          )}
        </details>

        <details className="panel report-details" open={detail.profile_gap_count > 0}>
          <summary>
            投标人资料缺口（{detail.profile_gap_count}）
            <Icon name="chevron-down" size={16} />
          </summary>
          {detail.profile_gaps.length ? (
            <ul className="compact-list">
              {detail.profile_gaps.map((gap, index) => (
                <li key={`${asText(gap.path)}-${index}`}>
                  <span>{asText(gap.label) || asText(gap.path) || '资料缺口'}</span>
                  <small>{asText(gap.placeholder)}</small>
                </li>
              ))}
            </ul>
          ) : (
            <p className="muted">常用资料未发现缺口。</p>
          )}
        </details>

        <details className="panel report-details">
          <summary>
            评分响应矩阵（{detail.response_matrix.length}）
            <Icon name="chevron-down" size={16} />
          </summary>
          {detail.response_matrix.length ? (
            <ul className="compact-list">
              {detail.response_matrix.map((row, index) => (
                <li key={`${row.factor}-${index}`}>
                  <span>{row.factor || '未命名评分项'}</span>
                  <small>
                    {row.category || '未分类'} · {formatScore(row.max_score)} 分 ·{' '}
                    {row.section_titles?.length
                      ? row.section_titles.join('、')
                      : '未映射章节'}
                  </small>
                </li>
              ))}
            </ul>
          ) : (
            <p className="muted">没有响应矩阵。</p>
          )}
        </details>

        <details className="panel report-details">
          <summary>
            自动修订记录（{detail.revisions.length}）
            <Icon name="chevron-down" size={16} />
          </summary>
          {detail.revisions.length ? (
            <ul className="compact-list">
              {detail.revisions.map((revision, index) => (
                <li key={index}>
                  <span>
                    {asText(revision.title) ||
                      asText(revision.section_id) ||
                      `修订 ${index + 1}`}
                  </span>
                  <small>
                    {asText(revision.reason) || asText(revision.summary) || '已自动修订'}
                  </small>
                </li>
              ))}
            </ul>
          ) : (
            <p className="muted">本产物未触发自动修订。</p>
          )}
        </details>
      </div>
    </section>
  )
}
