<template>
	<div :class="{ 'fixed left-0 right-0 z-20': isMobile }" :style="{ bottom: toolbarBottom }">
		<div
			class="flex flex-wrap justify-between gap-2 overflow-hidden pt-2.5"
			:class="{ 'pb-2.5': isMobile }"
		>
			<!-- Text editor buttons -->
			<div class="flex items-center gap-1 overflow-x-auto" :class="{ 'px-3': isMobile }">
				<TextEditorFixedMenu :buttons class="!bg-inherit" />
				<EmojiPicker
					v-if="!isMobile"
					v-slot="{ togglePopover }"
					@update:model-value="emit('appendEmoji', $event)"
				>
					<Button variant="ghost" class="max-h-6 max-w-6" @click="togglePopover()">
						<template #icon>
							<Laugh class="icon" />
						</template>
					</Button>
				</EmojiPicker>
				<Button variant="ghost" class="max-h-6 max-w-6" @click="fileInput?.click()">
					<template #icon>
						<Paperclip class="icon" />
					</template>
				</Button>
				<input
					ref="fileInput"
					type="file"
					class="hidden"
					multiple
					@change="onFilesSelected"
				/>

				<!-- S/MIME · OpenPGP -->
				<Button
					v-if="canSign"
					variant="ghost"
					class="max-h-6 max-w-6"
					:class="{ 'text-ink-green-3': sign }"
					:tooltip="sign ? __('Signing enabled') : __('Sign this message')"
					@click="emit('toggleSign')"
				>
					<template #icon>
						<ShieldCheck class="icon" />
					</template>
				</Button>
				<Button
					v-if="canSign"
					variant="ghost"
					class="max-h-6 max-w-6"
					:class="{ 'text-ink-blue-3': encrypt }"
					:disabled="!canEncrypt"
					:tooltip="
						canEncrypt
							? encrypt
								? __('Encryption enabled')
								: __('Encrypt this message')
							: __('No encryption key for one or more recipients')
					"
					@click="emit('toggleEncrypt')"
				>
					<template #icon>
						<Lock class="icon" />
					</template>
				</Button>
			</div>

			<!-- Send & Discard -->
			<div v-if="!isMobile" class="ml-auto flex items-center space-x-2">
				<Button
					:label="__('Discard')"
					:tooltip="__('Discard ({0}+D)', [modifier])"
					:icon-left="Trash2"
					@click="emit('discardMail')"
				/>
				<Button
					variant="solid"
					:label="__('Send')"
					:tooltip="__('Send ({0}+Enter)', [modifier])"
					:icon-left="SendHorizontal"
					:disabled="isRecipientsEmpty"
					@click="emit('sendMail')"
				/>
			</div>
		</div>
	</div>
</template>
<script setup lang="ts">
import { computed, useTemplateRef } from 'vue'
import { Laugh, Lock, Paperclip, SendHorizontal, ShieldCheck, Trash2 } from 'lucide-vue-next'
import { Button, TextEditorFixedMenu } from 'frappe-ui'

import { isMac } from '@/apps/mail/utils'
import { useScreenSize, useTextEditorButtons, useVisualViewport } from '@/apps/mail/utils/composables'
import EmojiPicker from '@/apps/mail/components/EmojiPicker.vue'

const { isRecipientsEmpty } = defineProps<{
	isRecipientsEmpty: boolean
	sign?: boolean
	encrypt?: boolean
	canSign?: boolean
	canEncrypt?: boolean
}>()

const emit = defineEmits([
	'appendEmoji',
	'selectFiles',
	'discardMail',
	'sendMail',
	'toggleSign',
	'toggleEncrypt',
])

const modifier = computed(() => (isMac ? '⌘' : 'Ctrl'))

// Make toolbar hover over keyboard on mobile

const { isMobile } = useScreenSize()
const { buttons } = useTextEditorButtons()

const toolbarBottom = useVisualViewport(
	(viewport) => `${window.innerHeight - viewport.height - viewport.offsetTop}px`,
)

const fileInput = useTemplateRef('fileInput')

const onFilesSelected = async (e: Event) => {
	const input = e.target as HTMLInputElement
	const files = Array.from(input.files ?? [])
	if (!files.length) return

	emit('selectFiles', files)
	input.value = ''
}
</script>

<!-- todo: file upload -> discard race condition (draft saved) -->
