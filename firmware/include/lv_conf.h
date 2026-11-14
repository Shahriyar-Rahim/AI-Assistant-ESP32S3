#ifndef LV_CONF_H
#define LV_CONF_H

#if 1 /* Set to 1 to enable configuration content */

#include <stdint.h>

/* Color depth: 16 (RGB565) */
#define LV_COLOR_DEPTH 16
#define LV_COLOR_16_SWAP 1

/* Memory settings */
#define LV_MEM_CUSTOM 0
#define LV_MEM_SIZE (32U * 1024U)

/* Logging */
#define LV_USE_LOG 0

#define LV_USE_TFT_ESPI 0

#endif /* Enable content */

#endif /* LV_CONF_H */