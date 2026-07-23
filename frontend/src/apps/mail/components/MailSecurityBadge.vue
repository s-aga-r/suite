<template>
	<span v-if="security && (security.signed || security.encrypted)" class="flex items-center gap-1">
		<Tooltip v-if="security.encrypted" :text="encryptedText">
			<Lock class="h-3.5 w-3.5 text-ink-blue-3" />
		</Tooltip>
		<Tooltip v-if="security.signed" :text="signedText">
			<component :is="signatureIcon" class="h-3.5 w-3.5" :class="signatureClass" />
		</Tooltip>
	</span>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { Lock, ShieldAlert, ShieldCheck, ShieldQuestion } from 'lucide-vue-next'
import { Tooltip } from 'frappe-ui'

import type { MailSecurity } from '@/apps/mail/types'

const { security } = defineProps<{ security?: MailSecurity | null }>()

const protocolLabel = computed(() => security?.protocol ?? '')

const encryptedText = computed(() => __('Encrypted ({0})', [protocolLabel.value]))

const signatureIcon = computed(() => {
	if (security?.signature_valid === true) return ShieldCheck
	if (security?.signature_valid === false) return ShieldAlert
	return ShieldQuestion
})

const signatureClass = computed(() => {
	if (security?.signature_valid === true) return 'text-ink-green-3'
	if (security?.signature_valid === false) return 'text-ink-red-3'
	return 'text-ink-gray-5'
})

const signedText = computed(() => {
	if (security?.signature_valid === false)
		return __('Invalid signature: this message may have been tampered with.')

	const who = security?.signer_name || security?.signer || __('unknown sender')
	if (security?.signature_valid === true)
		return security?.trusted
			? __('Signed by {0} ({1}) — verified & trusted', [who, protocolLabel.value])
			: __('Signed by {0} ({1}) — verified (untrusted certificate)', [who, protocolLabel.value])

	return __('Signature could not be verified')
})
</script>
