import { Inbox, Info, MessagesSquare, Tag, Users } from 'lucide-vue-next'

/** A category as the API names it: what `set_mails_category` takes, and a mail's `category`. */
export type MailCategory = 'primary' | 'promotions' | 'social' | 'updates' | 'forums'

// The categories mail is sorted into as it is first fetched (suite.mail.classification). Labels are
// functions: the translations are not loaded yet when this module is.
export const CATEGORIES = [
	{ value: 'primary', label: () => __('Primary'), icon: Inbox },
	{ value: 'promotions', label: () => __('Promotions'), icon: Tag },
	{ value: 'social', label: () => __('Social'), icon: Users },
	{ value: 'updates', label: () => __('Updates'), icon: Info },
	{ value: 'forums', label: () => __('Forums'), icon: MessagesSquare },
] as const satisfies readonly { value: MailCategory; [key: string]: unknown }[]

/** The keyword that marks a message as being in `category` on the server; a list is filtered by it. */
export const categoryKeyword = (category: MailCategory) => `category_${category}`
