import { useRef, useState, type ReactNode } from 'react'
import { AlertDialog, Badge, Button, Callout, Code, Dialog, Flex, IconButton, Select, Separator, Text, TextField } from '@radix-ui/themes'
import { CheckCircledIcon, CopyIcon, ExclamationTriangleIcon, GearIcon, PlusIcon, ReloadIcon, TrashIcon, UploadIcon } from '@radix-ui/react-icons'
import {
  ApiError,
  PROJECT_TYPES,
  createProject,
  deleteProject,
  rotateToken,
  uploadLogs,
  type ProjectSummary,
  type UploadResult,
} from './api'

const ADMIN_KEY = 'admin-key'
const SLUG = /^[a-z0-9][a-z0-9_.-]{0,63}$/ // PROJECT_ID on the server

function savedKey() {
  try {
    return sessionStorage.getItem(ADMIN_KEY) ?? ''
  } catch {
    return '' // storage can be blocked; the key then lives only in this dialog
  }
}

const message = (e: unknown) => (e instanceof ApiError ? (e.status === 401 ? 'Wrong or missing admin key.' : e.message) : String(e))

interface Props {
  projects: ProjectSummary[] | null
  /** Called after a change; `removedId` is set when a project was deleted. */
  onChanged: (removedId?: string) => void
}

/** Create, upload logs to, rotate the token of, and delete projects. Needs the server's ADMIN_API_KEY. */
export function ManageProjects({ projects, onChanged }: Props) {
  const [adminKey, setAdminKey] = useState(savedKey)
  const [tokens, setTokens] = useState<Record<string, string>>({}) // push tokens seen this session, in memory only
  const [notice, setNotice] = useState<{ color: 'jade' | 'red'; text: string } | null>(null)

  function changeKey(value: string) {
    setAdminKey(value)
    try {
      sessionStorage.setItem(ADMIN_KEY, value)
    } catch {
      /* the key is kept in this dialog only */
    }
  }

  const ok = (text: string) => setNotice({ color: 'jade', text })
  const fail = (e: unknown) => setNotice({ color: 'red', text: message(e) })

  return (
    <Dialog.Root>
      <Dialog.Trigger>
        <Button variant="soft" color="gray">
          <GearIcon /> Projects
        </Button>
      </Dialog.Trigger>
      <Dialog.Content maxWidth="640px">
        <Dialog.Title>Projects</Dialog.Title>
        <Dialog.Description size="2" color="gray" mb="3">
          Projects and their logs are stored in the database. Changing them needs the admin key.
        </Dialog.Description>

        <Flex direction="column" gap="3">
          <Field label="Admin key">
            <TextField.Root type="password" autoComplete="off" value={adminKey} onChange={(e) => changeKey(e.target.value)} />
          </Field>
          {notice && (
            <Callout.Root color={notice.color} size="1">
              <Callout.Icon>{notice.color === 'jade' ? <CheckCircledIcon /> : <ExclamationTriangleIcon />}</Callout.Icon>
              <Callout.Text style={{ whiteSpace: 'pre-wrap' }}>{notice.text}</Callout.Text>
            </Callout.Root>
          )}
          <Separator size="4" />
          {projects?.length === 0 && (
            <Text size="2" color="gray">
              No projects yet. Create the first one below.
            </Text>
          )}
          {projects?.map((p) => (
            <ProjectRow
              key={p.id}
              project={p}
              adminKey={adminKey}
              token={tokens[p.id]}
              onToken={(t) => setTokens((s) => ({ ...s, [p.id]: t }))}
              onOk={ok}
              onFail={fail}
              onChanged={onChanged}
            />
          ))}
          <Separator size="4" />
          <NewProject
            adminKey={adminKey}
            onCreated={(id, token) => {
              setTokens((s) => ({ ...s, [id]: token }))
              ok(`Project ${id} created. Push token (shown once, copy it now):\n${token}`)
              onChanged()
            }}
            onFail={fail}
          />
        </Flex>

        <Flex justify="end" mt="4">
          <Dialog.Close>
            <Button variant="soft" color="gray">
              Close
            </Button>
          </Dialog.Close>
        </Flex>
      </Dialog.Content>
    </Dialog.Root>
  )
}

interface RowProps {
  project: ProjectSummary
  adminKey: string
  token?: string
  onToken: (t: string) => void
  onOk: (text: string) => void
  onFail: (e: unknown) => void
  onChanged: (removedId?: string) => void
}

function ProjectRow({ project: p, adminKey, token, onToken, onOk, onFail, onChanged }: RowProps) {
  const [busy, setBusy] = useState(false)
  const [typed, setTyped] = useState('') // a push token pasted for an upload
  const fileInput = useRef<HTMLInputElement>(null)
  const pushToken = token ?? typed.trim()

  async function run(job: () => Promise<void>) {
    setBusy(true)
    try {
      await job()
    } catch (e) {
      onFail(e)
    } finally {
      setBusy(false)
    }
  }

  const upload = (file: File) =>
    run(async () => {
      const r: UploadResult = await uploadLogs(p.id, file, pushToken)
      onOk(`${p.id}: ${r.stored} new event(s) stored, ${r.duplicates} already there.`)
      onChanged()
    })

  const rotate = () =>
    run(async () => {
      const r = await rotateToken(p.id, adminKey)
      onToken(r.push_token)
      onOk(`New push token for ${p.id} (shown once, copy it now; the old one no longer works):\n${r.push_token}`)
    })

  const remove = () =>
    run(async () => {
      await deleteProject(p.id, adminKey)
      onOk(`Project ${p.id} and its logs were deleted.`)
      onChanged(p.id)
    })

  return (
    <Flex direction="column" gap="2">
      <Flex align="center" justify="between" gap="2" wrap="wrap">
        <Flex align="center" gap="2">
          <Text weight="medium">{p.title}</Text>
          <Code size="1" variant="ghost" color="gray">
            {p.id}
          </Code>
          <Badge color={p.status === 'ok' ? 'gray' : 'red'}>{p.status === 'ok' ? p.type : 'invalid'}</Badge>
        </Flex>
        <Flex gap="2">
          <input
            ref={fileInput}
            type="file"
            accept=".log,.txt,text/plain"
            hidden
            onChange={(e) => {
              const file = e.target.files?.[0]
              e.target.value = '' // the same file can be chosen again
              if (file) upload(file)
            }}
          />
          <Button size="1" variant="soft" disabled={busy || !pushToken} onClick={() => fileInput.current?.click()}>
            <UploadIcon /> Upload log
          </Button>
          <Button size="1" variant="soft" color="gray" disabled={busy || !adminKey} onClick={rotate}>
            <ReloadIcon /> New token
          </Button>
          <AlertDialog.Root>
            <AlertDialog.Trigger>
              <IconButton size="1" variant="soft" color="red" aria-label={`Delete ${p.title}`} disabled={busy || !adminKey}>
                <TrashIcon />
              </IconButton>
            </AlertDialog.Trigger>
            <AlertDialog.Content maxWidth="420px">
              <AlertDialog.Title>Delete {p.title}?</AlertDialog.Title>
              <AlertDialog.Description size="2">
                This permanently deletes the project and all of its stored logs. It cannot be undone.
              </AlertDialog.Description>
              <Flex gap="3" justify="end" mt="4">
                <AlertDialog.Cancel>
                  <Button variant="soft" color="gray">
                    Cancel
                  </Button>
                </AlertDialog.Cancel>
                <AlertDialog.Action>
                  <Button color="red" onClick={remove}>
                    Delete
                  </Button>
                </AlertDialog.Action>
              </Flex>
            </AlertDialog.Content>
          </AlertDialog.Root>
        </Flex>
      </Flex>
      {!token && (
        <TextField.Root
          size="1"
          type="password"
          autoComplete="off"
          placeholder="Push token (needed to upload a log file)"
          aria-label={`Push token for ${p.title}`}
          value={typed}
          onChange={(e) => setTyped(e.target.value)}
        />
      )}
      {token && (
        <Flex align="center" gap="2">
          <Text size="1" color="gray">
            Token known for this session
          </Text>
          <IconButton size="1" variant="ghost" color="gray" aria-label="Copy push token" onClick={() => navigator.clipboard?.writeText(token)}>
            <CopyIcon />
          </IconButton>
        </Flex>
      )}
    </Flex>
  )
}

function NewProject({ adminKey, onCreated, onFail }: { adminKey: string; onCreated: (id: string, token: string) => void; onFail: (e: unknown) => void }) {
  const [id, setId] = useState('')
  const [title, setTitle] = useState('')
  const [type, setType] = useState<string>(PROJECT_TYPES[0])
  const [timezone, setTimezone] = useState('UTC')
  const [busy, setBusy] = useState(false)
  const valid = SLUG.test(id) && title.trim().length > 0 && timezone.trim().length > 0

  async function submit() {
    setBusy(true)
    try {
      const created = await createProject({ id, title: title.trim(), type, timezone: timezone.trim() }, adminKey)
      onCreated(created.id, created.push_token)
      setId('')
      setTitle('')
    } catch (e) {
      onFail(e)
    } finally {
      setBusy(false)
    }
  }

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault()
        if (valid && adminKey && !busy) submit()
      }}
    >
      <Flex direction="column" gap="2">
        <Text weight="medium">New project</Text>
        <Flex gap="2" wrap="wrap">
          <Field label="Id (letters, digits, - _ .)">
            <TextField.Root value={id} color={id && !SLUG.test(id) ? 'red' : undefined} onChange={(e) => setId(e.target.value)} />
          </Field>
          <Field label="Title">
            <TextField.Root value={title} onChange={(e) => setTitle(e.target.value)} />
          </Field>
        </Flex>
        <Flex gap="2" wrap="wrap">
          <Field label="Log type">
            <Select.Root value={type} onValueChange={setType}>
              <Select.Trigger aria-label="Log type" />
              <Select.Content>
                {PROJECT_TYPES.map((t) => (
                  <Select.Item key={t} value={t}>
                    {t}
                  </Select.Item>
                ))}
              </Select.Content>
            </Select.Root>
          </Field>
          <Field label="Timezone (e.g. Asia/Kolkata)">
            <TextField.Root value={timezone} onChange={(e) => setTimezone(e.target.value)} />
          </Field>
        </Flex>
        <Flex justify="end">
          <Button type="submit" loading={busy} disabled={!valid || !adminKey}>
            <PlusIcon /> Create project
          </Button>
        </Flex>
      </Flex>
    </form>
  )
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <Flex direction="column" gap="1" flexGrow="1">
      <Text size="1" color="gray">
        {label}
      </Text>
      {children}
    </Flex>
  )
}
