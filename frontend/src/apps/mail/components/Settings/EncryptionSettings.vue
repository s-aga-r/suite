<template>
	<AppSettingsHeader :title="__('Encryption')">
		<template #actions>
			<Dropdown :options="newKeyOptions">
				<Button icon-left="plus" :label="__('New key')" />
			</Dropdown>
		</template>
	</AppSettingsHeader>
	<AppSettingsBody>
		<div v-if="keys?.data?.length" class="flex flex-col">
			<div
				v-for="key in keys.data"
				:key="key.name"
				class="hover:bg-surface-gray-1 -mx-2 flex items-center justify-between rounded px-3 py-2"
			>
				<div class="flex flex-col">
					<div class="flex items-center gap-2 text-base">
						<Badge :theme="key.protocol === 'S/MIME' ? 'blue' : 'green'" variant="subtle">
							{{ key.protocol }}
						</Badge>
						<span class="font-medium">{{ key.email }}</span>
						<Badge v-if="key.is_default" theme="gray" variant="subtle">{{ __('Default') }}</Badge>
					</div>
					<span class="text-ink-gray-5 mt-1 font-mono text-xs">{{ key.fingerprint }}</span>
				</div>
				<Dropdown :options="keyOptions(key)">
					<Button variant="ghost" @click.stop>
						<template #icon><Ellipsis class="text-ink-gray-5 h-4 w-4" /></template>
					</Button>
				</Dropdown>
			</div>
		</div>

		<div v-else class="text-ink-gray-6 flex flex-col space-y-2 text-sm">
			<p class="text-base-medium">{{ __('No S/MIME or OpenPGP keys yet.') }}</p>
			<p>
				{{
					__(
						'Add a key to sign and decrypt your mail. Import an S/MIME certificate (.p12) or generate an OpenPGP key.',
					)
				}}
			</p>
		</div>

		<!-- Import S/MIME (.p12) -->
		<Dialog
			v-model="showImportSMIME"
			:options="{ title: __('Import S/MIME certificate'), size: 'lg' }"
		>
			<template #body-content>
				<div class="flex flex-col space-y-4">
					<FormControl
						v-model="smimeForm.email"
						type="email"
						:label="__('Email address')"
						:placeholder="__('you@example.com')"
					/>
					<div>
						<label class="text-ink-gray-5 mb-1 block text-xs">{{ __('Certificate (.p12 / .pfx)') }}</label>
						<input type="file" accept=".p12,.pfx" @change="onP12Selected" />
					</div>
					<FormControl
						v-model="smimeForm.passphrase"
						type="password"
						:label="__('Passphrase')"
						:placeholder="__('Passphrase protecting the .p12')"
					/>
					<FormControl v-model="smimeForm.sign_by_default" type="checkbox" :label="__('Sign by default')" />
				</div>
			</template>
			<template #actions>
				<Button
					variant="solid"
					:label="__('Import')"
					:loading="importPkcs12.loading"
					:disabled="!smimeForm.content"
					@click="submitImportSMIME"
				/>
			</template>
		</Dialog>

		<!-- Generate OpenPGP -->
		<Dialog v-model="showGeneratePGP" :options="{ title: __('Generate OpenPGP key') }">
			<template #body-content>
				<div class="flex flex-col space-y-4">
					<FormControl v-model="pgpForm.name" type="text" :label="__('Name')" />
					<FormControl
						v-model="pgpForm.email"
						type="email"
						:label="__('Email address')"
						:placeholder="__('you@example.com')"
					/>
				</div>
			</template>
			<template #actions>
				<Button
					variant="solid"
					:label="__('Generate')"
					:loading="generatePgp.loading"
					:disabled="!pgpForm.email"
					@click="submitGeneratePGP"
				/>
			</template>
		</Dialog>
	</AppSettingsBody>
</template>

<script setup lang="ts">
import { inject, reactive, ref } from 'vue'
import { Ellipsis, Pin, Trash2 } from 'lucide-vue-next'
import { Badge, Button, Dialog, Dropdown, FormControl, createResource, useList } from 'frappe-ui'
import AppSettingsHeader from '@/components/settings/AppSettingsHeader.vue'
import AppSettingsBody from '@/components/settings/AppSettingsBody.vue'

import { raiseToast } from '@/apps/mail/utils'

const user = inject('$user')

const keys = useList({
	doctype: 'Mail Crypto Key',
	immediate: true,
	fields: ['name', 'email', 'protocol', 'fingerprint', 'is_default', 'sign_by_default'],
	filters: { user: user.data.name },
	cacheKey: ['mailCryptoKeys', user.data.name],
})

const showImportSMIME = ref(false)
const showGeneratePGP = ref(false)

const newKeyOptions = [
	{ label: __('Import S/MIME (.p12)'), onClick: () => (showImportSMIME.value = true) },
	{ label: __('Generate OpenPGP key'), onClick: () => (showGeneratePGP.value = true) },
]

const keyOptions = (key: { name: string; is_default: boolean }) => [
	{
		label: __('Set Default'),
		icon: Pin,
		condition: () => !key.is_default,
		onClick: () => keys.setValue.submit({ name: key.name, is_default: 1 }).then(() => keys.reload()),
	},
	{
		label: __('Delete'),
		icon: Trash2,
		theme: 'red',
		onClick: () => keys.delete.submit({ name: key.name }),
	},
]

// Import S/MIME

const smimeForm = reactive({
	email: '',
	passphrase: '',
	content: '',
	sign_by_default: true,
})

const onP12Selected = (e: Event) => {
	const file = (e.target as HTMLInputElement).files?.[0]
	if (!file) return
	const reader = new FileReader()
	reader.onload = () => {
		const result = reader.result as string
		smimeForm.content = result.split(',')[1] ?? '' // strip data: prefix -> base64
	}
	reader.readAsDataURL(file)
}

const importPkcs12 = createResource({
	url: 'suite.mail.api.crypto.import_pkcs12',
	onSuccess: () => {
		showImportSMIME.value = false
		smimeForm.content = ''
		keys.reload()
		raiseToast(__('Certificate imported.'), 'success')
	},
	onError: (e: { message: string }) => raiseToast(e.message, 'error'),
})

const submitImportSMIME = () =>
	importPkcs12.submit({
		email: smimeForm.email,
		content: smimeForm.content,
		passphrase: smimeForm.passphrase,
		is_default: true,
		sign_by_default: smimeForm.sign_by_default,
	})

// Generate OpenPGP

const pgpForm = reactive({ name: '', email: '' })

const generatePgp = createResource({
	url: 'suite.mail.api.crypto.generate_pgp_key',
	onSuccess: () => {
		showGeneratePGP.value = false
		keys.reload()
		raiseToast(__('OpenPGP key generated.'), 'success')
	},
	onError: (e: { message: string }) => raiseToast(e.message, 'error'),
})

const submitGeneratePGP = () => generatePgp.submit({ email: pgpForm.email, name: pgpForm.name })
</script>
