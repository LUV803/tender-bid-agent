export type Profile = Record<string, unknown>
export type ProfileObject = Record<string, unknown>

export const COMPANY_FIELDS = [
  ['name', '公司名称'],
  ['unified_social_credit_code', '统一社会信用代码'],
  ['legal_representative', '法定代表人'],
  ['registered_capital', '注册资本'],
  ['established_date', '成立日期'],
  ['address', '注册地址'],
  ['contact_person', '项目联系人'],
  ['phone', '联系电话'],
  ['email', '电子邮箱'],
] as const

export const QUALIFICATION_FIELDS = [
  ['name', '资质名称'],
  ['level', '等级'],
  ['certificate_no', '证书编号'],
  ['issuer', '发证机关'],
  ['valid_until', '有效期至'],
  ['note', '备注'],
] as const

export const CREDIT_FIELDS = [
  ['rating', '信用等级'],
  ['rating_agency', '评级机构'],
  ['bank_credit', '银行资信'],
  ['tax_rating', '纳税等级'],
] as const

export const SAFETY_FIELDS = [
  ['production_license_no', '安全生产许可证号'],
  ['valid_until', '有效期至'],
  ['accident_record', '事故记录'],
] as const

const PROFILE_TEMPLATE: Profile = {
  company: {
    name: '',
    unified_social_credit_code: '',
    legal_representative: '',
    registered_capital: '',
    established_date: '',
    address: '',
    contact_person: '',
    phone: '',
    email: '',
  },
  qualifications: [],
  similar_projects: [],
  personnel: [],
  financials: [],
  credit: {
    rating: '',
    rating_agency: '',
    bank_credit: '',
    tax_rating: '',
  },
  management_systems: [],
  safety: {
    production_license_no: '',
    valid_until: '',
    accident_record: '',
  },
  commitments: [],
  capabilities: [],
  notes: '',
}

export function emptyProfile(): Profile {
  return structuredClone(PROFILE_TEMPLATE)
}

export function isRecord(value: unknown): value is ProfileObject {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

export function asRecord(value: unknown): ProfileObject {
  return isRecord(value) ? value : {}
}

export function asText(value: unknown): string {
  if (typeof value === 'string') return value
  if (typeof value === 'number' && Number.isFinite(value)) return String(value)
  return ''
}

export function asRecordList(value: unknown): ProfileObject[] {
  return Array.isArray(value) ? value.filter(isRecord) : []
}

export function profileToJson(profile: Profile): string {
  return JSON.stringify(profile, null, 2)
}

export function parseProfileJson(
  text: string,
): { profile: Profile; error: null } | { profile: null; error: string } {
  try {
    const value: unknown = JSON.parse(text)
    if (!isRecord(value)) {
      return { profile: null, error: '顶层必须是 JSON 对象（{ ... }）' }
    }
    return { profile: value, error: null }
  } catch (reason) {
    const message = reason instanceof Error ? reason.message : String(reason)
    return { profile: null, error: `JSON 格式错误：${message}` }
  }
}

export function withCompanyField(
  profile: Profile,
  key: string,
  value: string,
): Profile {
  return {
    ...profile,
    company: { ...asRecord(profile.company), [key]: value },
  }
}

export function withNestedField(
  profile: Profile,
  section: 'credit' | 'safety',
  key: string,
  value: string,
): Profile {
  return {
    ...profile,
    [section]: { ...asRecord(profile[section]), [key]: value },
  }
}

export function withQualificationField(
  profile: Profile,
  index: number,
  key: string,
  value: string,
): Profile {
  const items = [...asRecordList(profile.qualifications)]
  const current = items[index] ?? {}
  items[index] = { ...current, [key]: value }
  return { ...profile, qualifications: items }
}

export function addQualification(profile: Profile): Profile {
  const items = [...asRecordList(profile.qualifications)]
  items.push(Object.fromEntries(QUALIFICATION_FIELDS.map(([key]) => [key, ''])))
  return { ...profile, qualifications: items }
}

export function removeQualification(profile: Profile, index: number): Profile {
  return {
    ...profile,
    qualifications: asRecordList(profile.qualifications).filter(
      (_, current) => current !== index,
    ),
  }
}

export function profileStats(profile: Profile): {
  filled: number
  total: number
  qualifications: number
} {
  const company = asRecord(profile.company)
  const credit = asRecord(profile.credit)
  const safety = asRecord(profile.safety)
  const items = asRecordList(profile.qualifications)

  let filled = 0
  let total = 0
  for (const [key] of COMPANY_FIELDS) {
    total += 1
    if (asText(company[key]).trim()) filled += 1
  }
  for (const [key] of CREDIT_FIELDS) {
    total += 1
    if (asText(credit[key]).trim()) filled += 1
  }
  for (const [key] of SAFETY_FIELDS) {
    total += 1
    if (asText(safety[key]).trim()) filled += 1
  }
  for (const item of items) {
    for (const [key] of QUALIFICATION_FIELDS) {
      total += 1
      if (asText(item[key]).trim()) filled += 1
    }
  }
  return { filled, total, qualifications: items.length }
}
