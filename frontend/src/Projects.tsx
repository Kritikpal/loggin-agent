import { useRef, useState, type ReactNode } from 'react'
import { AlertDialog, Badge, Button, Callout, Code, Dialog, Flex, IconButton, Select, Separator, Text, TextField } from '@radix-ui/themes'
import { CheckCircledIcon, ExclamationTriangleIcon, GearIcon, PlusIcon, TrashIcon, UploadIcon } from '@radix-ui/react-icons'
import {
  ApiError,
  PROJECT_TYPES,
  createProject,
  deleteProject,
  uploadLogs,
  type ProjectSummary,
  type UploadResult,
} from './api'

const SLUG = /^[a-z0-9][a-z0-9_.-]{0,63}$/ // PROJECT_ID on the server

const message = (e: unknown) => (e instanceof ApiError ? e.message : String(e))

interface Props {
  projects: ProjectSummary[] | null
  /** Called after a change; `removedId` is set when a project was deleted. */
  onChanged: (removedId?: string) => void
}

/** Create, upload logs to, and delete projects. Open: no keys. */
export function ManageProjects({ projects, onChanged }: Props) {
  const [notice, setNotice] = useState<{ color: 'jade' | 'red'; text: string } | null>(null)

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
          Projects and their logs are stored in the database.
        </Dialog.Description>

        <Flex direction="column" gap="3">
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
            <ProjectRow key={p.id} project={p} onOk={ok} onFail={fail} onChanged={onChanged} />
          ))}
          <Separator size="4" />
          <NewProject
            onCreated={(id) => {
              ok(`Project ${id} created. Upload a log file to it above.`)
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
  onOk: (text: string) => void
  onFail: (e: unknown) => void
  onChanged: (removedId?: string) => void
}

function ProjectRow({ project: p, onOk, onFail, onChanged }: RowProps) {
  const [busy, setBusy] = useState(false)
  const fileInput = useRef<HTMLInputElement>(null)

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
      const r: UploadResult = await uploadLogs(p.id, file)
      onOk(`${p.id}: ${r.stored} new event(s) stored, ${r.duplicates} already there.`)
      onChanged()
    })

  const remove = () =>
    run(async () => {
      await deleteProject(p.id)
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
          <Button size="1" variant="soft" disabled={busy} onClick={() => fileInput.current?.click()}>
            <UploadIcon /> Upload log
          </Button>
          <AlertDialog.Root>
            <AlertDialog.Trigger>
              <IconButton size="1" variant="soft" color="red" aria-label={`Delete ${p.title}`} disabled={busy}>
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
    </Flex>
  )
}

function NewProject({ onCreated, onFail }: { onCreated: (id: string) => void; onFail: (e: unknown) => void }) {
  const [id, setId] = useState('')
  const [title, setTitle] = useState('')
  const [type, setType] = useState<string>(PROJECT_TYPES[0])
  const [timezone, setTimezone] = useState('UTC')
  const [busy, setBusy] = useState(false)
  const valid = SLUG.test(id) && title.trim().length > 0 && timezone.trim().length > 0

  async function submit() {
    setBusy(true)
    try {
      const created = await createProject({ id, title: title.trim(), type, timezone: timezone.trim() })
      onCreated(created.id)
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
        if (valid && !busy) submit()
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
          <Button type="submit" loading={busy} disabled={!valid}>
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
